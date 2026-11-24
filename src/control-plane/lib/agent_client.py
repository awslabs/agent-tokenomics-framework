"""Client for the application plane (the support agent on AgentCore Runtime).

The one place that knows HOW to reach the agent: the bedrock-agentcore
`invoke_agent_runtime` API. The load generator passes a message + a synthetic
`user_id` (+ optional profile) and gets back the agent's JSON telemetry
unchanged. (Benchmark users are synthetic scenario identities, not real Cognito
users, so their order lookups simply return empty -- fine for cost benchmarking.)

Every request is signed with the benchmark KMS HMAC key (shared/caller_mac.py):
the runtime only accepts synthetic `bench-*` users and pinned flags / usage
plans from a signed request (threat model M8).

Reference:
  https://docs.aws.amazon.com/sdk-for-python/v1/reference/clients/bedrock-agentcore/operations/invoke_agent_runtime/
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

import config
from shared import caller_mac


class AgentError(RuntimeError):
    """Raised when the agent call fails or returns an unusable response."""


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "bedrock-agentcore",
        region_name=config.AWS_REGION,
        config=BotoConfig(read_timeout=config.AGENT_TIMEOUT_SECONDS, retries={"max_attempts": 2}),
    )


@lru_cache(maxsize=1)
def _kms():
    return boto3.client("kms", region_name=config.AWS_REGION)


def _sign(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach a fresh benchmark MAC so the runtime accepts this request."""
    exp = int(time.time()) + caller_mac.TTL_SECONDS
    resp = _kms().generate_mac(
        KeyId=config.BENCHMARK_MAC_KEY_ARN,
        Message=caller_mac.digest(payload, exp),
        MacAlgorithm=caller_mac.MAC_ALGORITHM,
    )
    mac = base64.b64encode(resp["Mac"]).decode("ascii")
    return {**payload, caller_mac.AUTH_FIELD: {"exp": exp, "mac": mac}}


def _session_id(session_id: str | None) -> str:
    """AgentCore requires a runtimeSessionId of 33+ chars; pad/generate one."""
    sid = session_id or f"sess-{uuid.uuid4().hex}"
    if len(sid) < 33:
        sid = f"{sid}-{uuid.uuid4().hex}"
    return sid[:128]


def invoke(
    message: str,
    user_id: str,
    *,
    profile: dict[str, Any] | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    flags: dict[str, Any] | None = None,
    usage_plans: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one turn against the AgentCore runtime and return its JSON response.

    Matches the entrypoint contract: message + user_id (+ optional profile).
    Response shape:
      { "reply": str, "usage": {...}, "identity": {...}, "model_ids": {...} }

    `flags` and `usage_plans`, when given, are pinned PER REQUEST so a benchmark
    run uses exactly that config regardless of the live tables (the entrypoint
    applies them as in-memory overrides and clears them after the turn -- it
    never writes the shared Flags / UsagePlans tables). Benchmarks pass a no-caps
    `usage_plans` so per-user quotas/throttle never fire during a run.
    """
    if not config.AGENT_RUNTIME_ARN:
        raise AgentError("AGENT_RUNTIME_ARN is not set; deploy the app plane first.")
    if not config.BENCHMARK_MAC_KEY_ARN:
        raise AgentError("BENCHMARK_MAC_KEY_ARN is not set; deploy the app plane first.")
    if not user_id.startswith(caller_mac.BENCH_USER_PREFIX):
        raise AgentError(f"benchmark user_id must start with {caller_mac.BENCH_USER_PREFIX!r}")

    payload: dict[str, Any] = {"message": message, "user_id": user_id}
    if profile is not None:
        payload["profile"] = profile
    if session_id:
        payload["session_id"] = session_id
    if run_id:
        payload["run_id"] = run_id
    if flags is not None:
        payload["flags"] = flags
    if usage_plans is not None:
        payload["usage_plans"] = usage_plans

    try:
        payload = _sign(payload)
        resp = _client().invoke_agent_runtime(
            agentRuntimeArn=config.AGENT_RUNTIME_ARN,
            runtimeSessionId=_session_id(session_id),
            qualifier=config.AGENT_RUNTIME_QUALIFIER,
            payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:  # boto/client errors -> a clean AgentError
        raise AgentError(f"invoke_agent_runtime failed: {type(exc).__name__}: {exc}") from exc

    try:
        body = resp["response"].read()
        data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
    except Exception as exc:
        raise AgentError(f"could not parse agent response: {exc}") from exc

    if isinstance(data, dict) and data.get("error"):
        raise AgentError(str(data["error"]))
    return data
