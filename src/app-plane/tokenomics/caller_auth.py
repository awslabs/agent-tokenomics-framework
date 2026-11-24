"""Who is this turn for? Verified at the runtime, not trusted from the payload.

Threat model M8 (T1, T2). The runtime uses IAM inbound auth, so any principal
with InvokeAgentRuntime can send any payload. Identity and governance overrides
are therefore only accepted with proof:

  - Customer turns (chat proxy) carry the user's Cognito id token. We verify its
    signature against the pool's JWKS plus issuer, audience, token_use and expiry,
    and take user_id + profile from the claims. Pinned flags / usage_plans are
    rejected on this path.
  - Benchmark turns carry a KMS HMAC over the payload (shared.caller_mac). Only
    the benchmark worker can sign. These may pin flags / usage_plans but only act
    as synthetic `bench-*` users, never a real customer.

Anything else is rejected.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import boto3
import jwt
from botocore.exceptions import BotoCoreError, ClientError

from shared import caller_mac

logger = logging.getLogger(__name__)

_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
BENCHMARK_MAC_KEY_ARN = os.environ.get("BENCHMARK_MAC_KEY_ARN", "")

_LEEWAY_SECONDS = 30


class CallerAuthError(Exception):
    """The request didn't prove who it is for (logged; caller sees a generic error)."""


@dataclass
class Caller:
    user_id: str
    profile: dict[str, Any] = field(default_factory=dict)
    # True only for signed benchmark requests: may pin flags / usage_plans.
    may_pin_config: bool = False


def _issuer() -> str:
    return f"https://cognito-idp.{_REGION}.amazonaws.com/{USER_POOL_ID}"


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    # Caches signing keys in-process; Cognito rotates them rarely.
    return jwt.PyJWKClient(f"{_issuer()}/.well-known/jwks.json", cache_keys=True, timeout=5)


@lru_cache(maxsize=1)
def _kms():
    return boto3.client("kms", region_name=_REGION)


def _profile(claims: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": claims.get("name") or claims.get("email") or "Customer",
        "email": claims.get("email", ""),
        "tier": claims.get("custom:tier", "basic"),
    }


def verify_id_token(token: str) -> dict[str, Any]:
    """Return the claims of a valid id token from this deployment's user pool."""
    if not USER_POOL_ID or not CLIENT_ID:
        raise CallerAuthError("Cognito pool not configured")
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=_issuer(),
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise CallerAuthError(f"invalid id token: {type(exc).__name__}") from exc
    if claims.get("token_use") != "id":
        raise CallerAuthError("not an id token")
    return claims


def verify_benchmark(payload: dict[str, Any]) -> None:
    """Raise unless the payload carries a fresh, valid benchmark MAC."""
    if not BENCHMARK_MAC_KEY_ARN:
        raise CallerAuthError("benchmark key not configured")
    auth = payload.get(caller_mac.AUTH_FIELD)
    if not isinstance(auth, dict):
        raise CallerAuthError("missing benchmark auth")
    exp, mac = auth.get("exp"), auth.get("mac")
    now = int(time.time())
    if not isinstance(exp, int) or not now <= exp <= now + caller_mac.TTL_SECONDS + _LEEWAY_SECONDS:
        raise CallerAuthError("benchmark auth expired or out of range")
    try:
        mac_bytes = base64.b64decode(mac, validate=True)
        resp = _kms().verify_mac(
            KeyId=BENCHMARK_MAC_KEY_ARN,
            Message=caller_mac.digest(payload, exp),
            MacAlgorithm=caller_mac.MAC_ALGORITHM,
            Mac=mac_bytes,
        )
    except (ValueError, TypeError, ClientError, BotoCoreError) as exc:
        raise CallerAuthError(f"benchmark MAC rejected: {type(exc).__name__}") from exc
    if not resp.get("MacValid"):
        raise CallerAuthError("benchmark MAC invalid")


def authenticate(payload: dict[str, Any]) -> Caller:
    """Resolve the verified caller for this turn, or raise CallerAuthError."""
    token = payload.get("id_token")
    if token:
        if not isinstance(token, str):
            raise CallerAuthError("invalid id token")
        if payload.get("flags") is not None or payload.get("usage_plans") is not None:
            raise CallerAuthError("customer turns may not pin flags or usage plans")
        claims = verify_id_token(token)
        user_id = claims["sub"]
        # The chat proxy derives the session from the sub; never let a caller
        # write into (or inflate the counter of) another user's session.
        session_id = payload.get("session_id")
        if session_id and not str(session_id).startswith(f"{user_id}-"):
            raise CallerAuthError("session does not belong to the user")
        return Caller(user_id=user_id, profile=_profile(claims))

    if caller_mac.AUTH_FIELD in payload:
        verify_benchmark(payload)
        user_id = (payload.get("user_id") or "").strip()
        if not user_id.startswith(caller_mac.BENCH_USER_PREFIX):
            raise CallerAuthError("benchmark turns must use a bench-* user")
        profile = payload.get("profile")
        return Caller(
            user_id=user_id,
            profile=profile if isinstance(profile, dict) else {},
            may_pin_config=True,
        )

    raise CallerAuthError("no caller credentials")
