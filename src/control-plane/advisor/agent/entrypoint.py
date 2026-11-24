"""AgentCore Runtime entrypoint for the Tokenomics Advisor.

`BedrockAgentCoreApp` provides /invocations (POST) and /ping (GET) on port 8080;
this module supplies the one entrypoint. Invoked only by the AdvisorWorker
Lambda (IAM-authorised InvokeAgentRuntime).

Payload:  {"message": str, "history": [{"role", "text"}], "window": str, "user": str,
           "retry_note"?: str, "deadline"?: int}   (the last two only on the worker's one retry)
Response: {"outcome", "narrative", "patterns_applied", "tool_calls", "stop_reason", "usage"}
          (+ "title", "table" when answered)
          or {"error": "<generic message>"}
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from advisor import data_tools as advisor_tools
from shared.logging_setup import configure_logging

from . import settings
from .agent import run_turn

configure_logging()  # make module loggers (INFO+) reach CloudWatch

app = BedrockAgentCoreApp()


logger = logging.getLogger(__name__)


def validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > settings.MAX_MESSAGE_CHARS:
        raise ValueError("invalid message")
    window = payload.get("window") or "7d"
    if window not in settings.WINDOWS:
        raise ValueError("invalid window")
    user = payload.get("user") or "all"
    if not isinstance(user, str) or len(user) > 254:
        raise ValueError("invalid user")
    history = payload.get("history") or []
    if not isinstance(history, list) or len(history) > settings.MAX_HISTORY_MESSAGES or len(history) % 2:
        raise ValueError("invalid history")
    for i, h in enumerate(history):
        role = "user" if i % 2 == 0 else "assistant"
        if not isinstance(h, dict) or h.get("role") != role or not isinstance(h.get("text"), str):
            raise ValueError("invalid history")
    retry_note = payload.get("retry_note") or ""
    if not isinstance(retry_note, str) or len(retry_note) > 300:
        raise ValueError("invalid retry_note")
    deadline = payload.get("deadline")
    if deadline is not None and (
        not isinstance(deadline, int) or not 30 <= deadline <= settings.DEADLINE_SECONDS
    ):
        raise ValueError("invalid deadline")
    return {
        "message": message.strip(),
        "window": window,
        "user": user,
        "retry_note": retry_note,
        "deadline": deadline,
        "history": [{"role": h["role"], "text": h["text"][:8000]} for h in history],
    }


@app.entrypoint
def invoke(payload: dict) -> dict:
    try:
        req = validate(payload)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        return run_turn(
            req["message"],
            req["history"],
            req["window"],
            req["user"],
            retry_note=req["retry_note"],
            deadline=req["deadline"],
        )
    except Exception:  # noqa: BLE001 -- keep the runtime resilient; no internals out
        logger.warning("[advisor-agent] invocation failed\n%s", traceback.format_exc())
        return {"error": "advisor failed"}


if __name__ == "__main__":
    try:
        advisor_tools.warm()  # clients + catalog once, before tool calls run in parallel
    except Exception:  # noqa: BLE001 -- the tools create them lazily if this fails
        logger.warning("[advisor-agent] warm-up failed\n%s", traceback.format_exc())
    app.run()
