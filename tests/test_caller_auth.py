"""Unit tests for runtime caller verification (threat model M8, T1/T2). No AWS.

The runtime must take identity only from a verified Cognito id token, and accept
synthetic users / pinned governance config only from a KMS-signed benchmark
request. Cognito's JWKS and KMS are faked: a local RSA key signs the tokens and a
local HMAC stands in for GenerateMac / VerifyMac.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (_SRC, _SRC / "app-plane"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from shared import caller_mac  # noqa: E402
from tokenomics import caller_auth  # noqa: E402
from tokenomics.caller_auth import CallerAuthError, authenticate  # noqa: E402

POOL_ID = "us-east-1_TestPool"
CLIENT_ID = "test-client-id"
KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/test"
SUB = "0b7e4c2a-1111-2222-3333-444455556666"

_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_HMAC_SECRET = b"test-secret"


class _FakeKms:
    """GenerateMac / VerifyMac over a local secret."""

    def generate_mac(self, KeyId, Message, MacAlgorithm):
        return {"Mac": hmac.new(_HMAC_SECRET, Message, hashlib.sha256).digest()}

    def verify_mac(self, KeyId, Message, MacAlgorithm, Mac):
        good = hmac.new(_HMAC_SECRET, Message, hashlib.sha256).digest()
        return {"MacValid": hmac.compare_digest(good, Mac)}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(caller_auth, "_REGION", "us-east-1")
    monkeypatch.setattr(caller_auth, "USER_POOL_ID", POOL_ID)
    monkeypatch.setattr(caller_auth, "CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(caller_auth, "BENCHMARK_MAC_KEY_ARN", KEY_ARN)
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=_RSA.public_key()))
    monkeypatch.setattr(caller_auth, "_jwks_client", lambda: jwks)
    monkeypatch.setattr(caller_auth, "_kms", lambda: _FakeKms())


def _token(key=_RSA, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": SUB,
        "aud": CLIENT_ID,
        "iss": f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}",
        "token_use": "id",  # nosec B105 -- JWT claim value
        "iat": now,
        "exp": now + 3600,
        "email": "bob@example.com",
        "name": "Bob",
        "custom:tier": "prime",
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


def _signed(payload: dict, exp: int | None = None) -> dict:
    exp = exp if exp is not None else int(time.time()) + caller_mac.TTL_SECONDS
    mac = _FakeKms().generate_mac(KeyId=KEY_ARN, Message=caller_mac.digest(payload, exp), MacAlgorithm="x")
    return {**payload, caller_mac.AUTH_FIELD: {"exp": exp, "mac": base64.b64encode(mac["Mac"]).decode()}}


# ---- customer turns (id token) ---------------------------------------------


def test_identity_comes_from_verified_token_not_payload():
    caller = authenticate(
        {
            "message": "hi",
            "id_token": _token(),
            "user_id": "someone-else",  # ignored
            "profile": {"tier": "enterprise"},  # ignored
            "session_id": f"{SUB}-default",
        }
    )
    assert caller.user_id == SUB
    assert caller.profile == {"name": "Bob", "email": "bob@example.com", "tier": "prime"}
    assert caller.may_pin_config is False


@pytest.mark.parametrize(
    "token",
    [
        _token(key=_OTHER_RSA),  # forged signature
        _token(exp=int(time.time()) - 3600),  # expired
        _token(aud="other-client"),  # another app's token
        _token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"),  # another pool
        _token(token_use="access"),  # access token, not id  # nosec B106 -- JWT claim value
        "not-a-jwt",
    ],
)
def test_bad_tokens_rejected(token):
    with pytest.raises(CallerAuthError):
        authenticate({"message": "hi", "id_token": token})


@pytest.mark.parametrize("field", ["flags", "usage_plans"])
def test_customer_turn_cannot_pin_config(field):
    with pytest.raises(CallerAuthError):
        authenticate({"message": "hi", "id_token": _token(), field: {}})


def test_customer_turn_cannot_use_another_users_session():
    with pytest.raises(CallerAuthError):
        authenticate({"message": "hi", "id_token": _token(), "session_id": "victim-sub-default"})


def test_no_credentials_rejected():
    # The pre-M8 contract: caller-asserted user_id + pinned no-caps plans.
    with pytest.raises(CallerAuthError):
        authenticate({"message": "hi", "user_id": SUB, "usage_plans": {}})


def test_unconfigured_pool_fails_closed(monkeypatch):
    monkeypatch.setattr(caller_auth, "USER_POOL_ID", "")
    with pytest.raises(CallerAuthError):
        authenticate({"message": "hi", "id_token": _token()})


# ---- benchmark turns (KMS MAC) ---------------------------------------------


def _bench_payload(**overrides) -> dict:
    payload = {
        "message": "where is my order?",
        "user_id": "bench-user-1",
        "profile": {"name": "bench-user-1"},
        "run_id": "run-1",
        "flags": {"prompt_cache": {"enabled": True}},
        "usage_plans": {},
    }
    payload.update(overrides)
    return payload


def test_signed_benchmark_may_pin_config():
    caller = authenticate(_signed(_bench_payload()))
    assert caller.user_id == "bench-user-1"
    assert caller.may_pin_config is True


def test_tampered_benchmark_payload_rejected():
    signed = _signed(_bench_payload())
    signed["flags"] = {"model_routing": {"enabled": False}}
    with pytest.raises(CallerAuthError):
        authenticate(signed)


def test_benchmark_cannot_act_as_real_customer():
    with pytest.raises(CallerAuthError):
        authenticate(_signed(_bench_payload(user_id=SUB)))


@pytest.mark.parametrize(
    "exp",
    [int(time.time()) - 1, int(time.time()) + 24 * 3600],  # expired / too far ahead
)
def test_benchmark_expiry_enforced(exp):
    with pytest.raises(CallerAuthError):
        authenticate(_signed(_bench_payload(), exp=exp))


def test_benchmark_garbage_mac_rejected():
    payload = _bench_payload()
    payload[caller_mac.AUTH_FIELD] = {"exp": int(time.time()) + 60, "mac": "%%%"}
    with pytest.raises(CallerAuthError):
        authenticate(payload)


def test_digest_ignores_key_order_and_auth_field():
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1, caller_mac.AUTH_FIELD: {"mac": "z"}}
    assert caller_mac.digest(a, 5) == caller_mac.digest(b, 5)
    assert caller_mac.digest(a, 5) != caller_mac.digest(a, 6)


# ---- signer (control plane) round-trips with the verifier ------------------


def test_agent_client_signs_requests_the_runtime_accepts(cp_import, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:runtime")
    monkeypatch.setenv("BENCHMARK_MAC_KEY_ARN", KEY_ARN)
    agent_client = cp_import("lib.agent_client")
    sent = {}

    def invoke_agent_runtime(**kw):
        sent.update(json.loads(kw["payload"]))
        return {"response": SimpleNamespace(read=lambda: b'{"reply": "ok"}')}

    monkeypatch.setattr(agent_client, "_kms", lambda: _FakeKms())
    monkeypatch.setattr(
        agent_client, "_client", lambda: SimpleNamespace(invoke_agent_runtime=invoke_agent_runtime)
    )

    agent_client.invoke("hi", "bench-user-2", flags={}, usage_plans={})
    assert authenticate(sent).may_pin_config is True

    with pytest.raises(agent_client.AgentError):
        agent_client.invoke("hi", SUB)  # never signs for a real customer


# ---- chat proxy forwards the token, not identity ---------------------------


def test_chat_proxy_forwards_id_token(monkeypatch):
    path = _SRC / "app-plane" / "chat_api" / "handler.py"
    spec = importlib.util.spec_from_file_location("chat_handler_m8", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = {}

    def invoke_agent_runtime(**kw):
        sent.update(json.loads(kw["payload"]))
        return {"response": SimpleNamespace(read=lambda: b'{"reply": "ok"}')}

    monkeypatch.setattr(mod, "_RUNTIME_ARN", "arn:test")
    monkeypatch.setattr(mod, "_client", lambda: SimpleNamespace(invoke_agent_runtime=invoke_agent_runtime))
    token = _token()
    event = {
        "httpMethod": "POST",
        "headers": {"authorization": f"Bearer {token}"},
        "body": json.dumps({"message": "hi"}),
        "requestContext": {"authorizer": {"claims": {"sub": SUB}}},
    }
    assert mod.handler(event, None)["statusCode"] == 200
    assert sent["id_token"] == token
    assert "user_id" not in sent and "profile" not in sent
    assert authenticate(sent).user_id == SUB  # the session id passes the owner check
