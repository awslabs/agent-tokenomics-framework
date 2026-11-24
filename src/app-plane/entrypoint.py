"""AgentCore Runtime entrypoint for the support agent.

The Amazon Bedrock AgentCore Python SDK (`BedrockAgentCoreApp`) provides the
mandatory `/invocations` POST and `/ping` GET endpoints on port 8080 for us; we
only supply the `@app.entrypoint` function that runs one customer turn.

Invocation contract (runtime payload), one of:
    customer turn (chat proxy):
      { "message": str, "id_token": str, "session_id"?: str }
      user_id + profile come from the VERIFIED id token (`sub`, claims).
    benchmark turn (benchmark worker, KMS-signed; see shared/caller_mac.py):
      { "message": str, "user_id": "bench-*", "profile"?: {...},
        "session_id"?: str, "run_id"?: str, "flags"?: {...},
        "usage_plans"?: {...}, "bench_auth": {"exp": int, "mac": str} }
    Anything else is rejected (tokenomics/caller_auth.py, threat model M8).

Response (the shape the control plane + chat API parse):
    { "reply": str, "usage": {...}, "identity": {...}, "model_ids": {...},
      "governance": {...} }

Deploy: packaged into an ARM64 container (see Dockerfile) and launched onto
AgentCore Runtime via the AgentCore CLI (see scripts/deploy-app-plane.sh).
Reference: https://strandsagents.com/docs/user-guide/deploy/deploy_to_bedrock_agentcore/python/
"""

from __future__ import annotations

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import config
from agents.orchestrator.agent import handle_turn
from shared.flags_schema import FlagConfig
from shared.logging_setup import configure_logging
from shared.usage_plans_schema import GovernanceConfig
from tokenomics import flags as flags_mod
from tokenomics import plans as plans_mod
from tokenomics.caller_auth import CallerAuthError, authenticate
from tokenomics.identity import RunContext

configure_logging()  # make module loggers (INFO+) reach CloudWatch
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


def _error(message: str) -> dict:
    return {"reply": "", "error": message, "usage": {}, "identity": {}, "model_ids": {}, "governance": {}}


@app.entrypoint
def invoke(payload: dict) -> dict:
    """Run one customer turn and return the reply plus tokenomics telemetry."""
    payload = payload or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return _error("message is required")
    if len(message) > config.MAX_MESSAGE_CHARS:
        return _error(f"message is longer than {config.MAX_MESSAGE_CHARS} characters")
    try:
        caller = authenticate(payload)
    except CallerAuthError as exc:
        logger.warning("[entrypoint] rejected caller: %s", exc)
        return _error("unauthorized")

    # A signed benchmark request may pin an exact flag config so the whole run
    # uses one known configuration regardless of the live flag table. Customer
    # turns can't (authenticate rejects them) and read the shared flag store.
    pinned = payload.get("flags") if caller.may_pin_config else None
    if pinned is not None:
        flags_mod.set_override(FlagConfig.from_dict(pinned))
    else:
        flags_mod.set_override(None)

    # A benchmark worker may likewise pin an exact usage-plans config so a run
    # A/B's baseline-vs-enforced deterministically, regardless of the live table.
    pinned_plans = payload.get("usage_plans") if caller.may_pin_config else None
    if pinned_plans is not None:
        plans_mod.set_override(GovernanceConfig.from_dict(pinned_plans))
    else:
        plans_mod.set_override(None)

    try:
        ctx = RunContext(
            user_id=caller.user_id,
            profile=caller.profile,
            run_id=payload.get("run_id"),
        )
        if payload.get("session_id"):
            ctx.session_id = payload["session_id"]
        result = handle_turn(message, ctx)
    except Exception:  # keep the runtime resilient; no internals out
        # Full traceback to CloudWatch; the caller gets a non-empty generic
        # `error` -- the chat API maps it to a 5xx so the UI shows the failure
        # instead of an empty reply.
        logger.exception("[entrypoint] turn failed")
        return _error("agent turn failed")
    finally:
        flags_mod.set_override(None)
        plans_mod.set_override(None)

    return {
        "reply": result.reply,
        "usage": result.usage,
        "identity": result.identity,
        "model_ids": result.model_ids,
        "governance": result.governance or {},
    }


if __name__ == "__main__":
    app.run()
