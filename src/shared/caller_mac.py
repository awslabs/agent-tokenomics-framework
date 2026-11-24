"""Signed benchmark requests to the support agent runtime (threat model M8).

The benchmark worker invokes the support runtime with synthetic users and pins
`flags` / `usage_plans` per request. The runtime accepts those only when the
request carries a KMS HMAC over the payload: the worker holds kms:GenerateMac on
the key, the runtime holds kms:VerifyMac, and nobody else holds either. So an
InvokeAgentRuntime grant alone can't pin a no-caps config.

Both planes import this module so the signer and the verifier hash the exact
same bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MAC_ALGORITHM = "HMAC_SHA_256"
# Payload field that carries {"exp": <epoch seconds>, "mac": <base64>}.
AUTH_FIELD = "bench_auth"
# Signed requests may only act as synthetic users, never a real Cognito sub.
BENCH_USER_PREFIX = "bench-"
# Signer TTL, and the furthest-future exp the verifier accepts.
TTL_SECONDS = 300


def digest(payload: dict[str, Any], exp: int) -> bytes:
    """SHA-256 of the canonical payload (minus the auth field) plus its expiry.

    KMS MACs at most 4 KiB, so we MAC this 32-byte digest, not the payload.
    """
    body = {k: v for k, v in payload.items() if k != AUTH_FIELD}
    canonical = json.dumps(
        {"payload": body, "exp": exp}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()
