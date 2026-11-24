"""AdvisorWorker Lambda -- answers one pending Advisor message.

Async-invoked (InvocationType="Event") by POST /advisor/chats with
{"owner", "chat_id", "reply_sk"}. It claims the pending reply exactly once
(async deliveries can repeat), loads the chat's grounded history, invokes the
Advisor agent on AgentCore, verifies the answer with the deterministic grounding
(advisor/grounding.py) and stores the result. One structured log line per
turn; prompt and reply text are never logged.
"""

from __future__ import annotations

import logging

# --- import bootstrap: make `handlers`, `lib`, `config`, `shared` importable ---
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CP_ROOT = os.path.dirname(_HERE)  # src/control-plane
if _CP_ROOT not in sys.path:
    sys.path.insert(0, _CP_ROOT)
# ------------------------------------------------------------------------------

import json
import re
import time
from typing import Any

from advisor import chats, grounding
from advisor import client as advisor_client
from shared.logging_setup import configure_logging

configure_logging()  # Lambda root handler: set our level so INFO logs reach CloudWatch

# A reply that fails grounding is retried once, telling the agent what was rejected, when the
# Lambda still has time: the retry's agent deadline leaves RETRY_MARGIN_SECONDS for an in-flight
# model step plus grounding and the store write.
# (An answer that ran out of agent steps or was cut off at the output cap -- outcome no_narrative --
# is retried the same way.)
RETRY_MARGIN_SECONDS = 60
MIN_RETRY_DEADLINE_SECONDS = 40
# The retry's read timeout ends this long before the Lambda does, so a slow retry raises
# AdvisorTimeout (session stopped) and the first result is still stored.
RETRY_READ_MARGIN_SECONDS = 15
_NOTE_TOKEN_UNSAFE = re.compile(r"[^\w.,%×$:/ -]")


logger = logging.getLogger(__name__)


def _retry_note(failure: str) -> str:
    code, _, detail = failure.partition(":")
    if code == "no_narrative":
        return (
            "A previous draft ran out of steps; call all data tools together in one step and all "
            "search_patterns together in the next, then answer."
        )
    if code == "max_tokens":
        return (
            "A previous draft was too long and was cut off; answer in a few short sentences and put any "
            "breakdown in the table."
        )
    if code == "typed_figure" and detail.startswith("number_words"):
        phrase = _NOTE_TOKEN_UNSAFE.sub("", detail.partition(":")[2])[:20]
        why = f'it spelled a number in words ("{phrase}")' if phrase else "it spelled a number in words"
    elif code == "typed_figure":
        why = f'it typed "{_NOTE_TOKEN_UNSAFE.sub("", detail)[:20]}" itself'
    elif code == "savings_projection":
        why = "it projected a saving"
    else:
        why = "it cited a figure reference that no tool returned"
    return (
        f"A previous draft was not shown because {why}; cite every figure as a figure reference "
        "from this turn's tool results."
    )


def _retry_budget(context: Any) -> tuple[int, int | None]:
    """-> (the retry's agent deadline, its read timeout; None = the client default)."""
    if not context:
        return 180 - RETRY_MARGIN_SECONDS, None
    remaining = context.get_remaining_time_in_millis() / 1000
    return (
        int(min(remaining - RETRY_MARGIN_SECONDS, 150)),
        int(min(remaining - RETRY_READ_MARGIN_SECONDS, advisor_client.READ_TIMEOUT_SECONDS)),
    )


def _result_for(data: dict[str, Any], message: str) -> dict[str, Any]:
    outcome = data.get("outcome")
    calls = data.get("tool_calls") or []
    if outcome == "answered":
        return grounding.ground(
            data.get("narrative", ""),
            data.get("patterns_applied") or [],
            calls,
            message,
            data.get("table") or [],
        )
    if outcome == "declined":
        return grounding.declined()
    if outcome == "timeout":
        return grounding.fallback_reply(grounding.NOTE_TIMEOUT, calls)
    return grounding.fallback_reply(grounding.NOTE_UNVERIFIED if calls else grounding.NOTE_FAILED, calls)


def _log(**fields: Any) -> None:
    print(json.dumps({"event": "advisor_turn", **fields}, default=str))


# CloudWatch Embedded Metric Format: Lambda turns the turn log line into the fleet-wide metric
# Tokenomics/Advisor KbFallbacks (no dimensions), so a KB that keeps falling back can be alarmed on.
_KB_FALLBACK_METRIC = {
    "Namespace": "Tokenomics/Advisor",
    "Dimensions": [[]],
    "Metrics": [{"Name": "KbFallbacks", "Unit": "Count"}],
}


def _kb_fallbacks(data: dict[str, Any]) -> int:
    """search_patterns calls in one agent attempt that used the keyword fallback instead of the KB."""
    return sum(
        1
        for c in data.get("tool_calls") or []
        if c.get("tool") == "search_patterns" and (c.get("result") or {}).get("source") == "fallback"
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    event = event if isinstance(event, dict) else {}
    owner, chat_id, reply_sk = (str(event.get(k) or "") for k in ("owner", "chat_id", "reply_sk"))
    if not (
        grounding._SUB_RE.match(owner)
        and grounding._UUID_RE.match(chat_id)
        and reply_sk.startswith(f"MSG#{chat_id}#")
    ):
        _log(outcome="rejected_event")
        return {"ok": False}
    try:
        claimed = chats.claim_reply(owner, reply_sk)
    except chats.ReplyExpired:
        # Queued too long: fail it without running the agent (the UI would soon show it stale).
        stored = chats.finish_reply(owner, chat_id, reply_sk, "failed", {"reply": grounding.NOTE_FAILED})
        _log(outcome="expired", chat_id=chat_id, owner=owner, status="failed", stored=stored)
        return {"ok": False}
    if not claimed:
        _log(outcome="duplicate_delivery", chat_id=chat_id)
        return {"ok": True, "skipped": True}
    started = time.monotonic()
    status, outcome, data, result = "failed", "error", {}, {"reply": grounding.NOTE_FAILED}
    retried, kb_fallbacks = None, 0
    try:
        message = str(claimed.get("request_text") or "")
        history = grounding.history_for_agent(
            [m for m in chats.messages(owner, chat_id) if m["sk"] < reply_sk]
        )
        payload = {
            "message": message,
            "history": history,
            "window": claimed.get("window") or "7d",
            "user": claimed.get("user_focus") or "all",
        }
        try:
            data = advisor_client.invoke(payload, advisor_client.session_id(owner, chat_id))
            kb_fallbacks += _kb_fallbacks(data)
            outcome = str(data.get("outcome") or "")
            result = _result_for(data, message)
            status = "complete"
            if not history and data.get("title"):
                try:
                    chats.set_title(owner, chat_id, grounding.clean_title(str(data["title"])))
                except Exception as exc:  # noqa: BLE001 -- a title is cosmetic
                    logger.warning("[advisor-worker] title not set (%s)", type(exc).__name__)
            failure = str(result.get("failure") or "") if outcome == "answered" else outcome
            if outcome == "no_narrative" and data.get("stop_reason") == "max_tokens":
                failure = "max_tokens"  # cut off at the output cap: ask for a shorter answer
            deadline, read_timeout = _retry_budget(context)
            if (
                (outcome == "answered" and not result.get("grounded")) or outcome == "no_narrative"
            ) and deadline >= MIN_RETRY_DEADLINE_SECONDS:
                retried = f"failed:{failure}"
                try:
                    again = advisor_client.invoke(
                        {**payload, "retry_note": _retry_note(failure), "deadline": deadline},
                        advisor_client.session_id(owner, chat_id),
                        read_timeout=read_timeout,
                    )
                    kb_fallbacks += _kb_fallbacks(again)  # counted even when the retry is discarded
                    if again.get("outcome") == "answered":
                        again_result = _result_for(again, message)
                        if again_result.get("grounded"):
                            data, result, retried = again, again_result, f"grounded:{failure}"
                except Exception as exc:  # noqa: BLE001 -- keep the first (fallback) result
                    logger.warning("[advisor-worker] retry failed (%s)", type(exc).__name__)
        except advisor_client.AdvisorTimeout:
            status, outcome = "complete", "timeout"
            result = grounding.fallback_reply(grounding.NOTE_TIMEOUT, [])
    except Exception as exc:  # noqa: BLE001 -- record a clean failure, never a fabricated answer
        outcome = f"error:{type(exc).__name__}"
        status, result = "failed", {"reply": grounding.NOTE_FAILED}
    finally:
        # Always reach a terminal state so the chat never stays locked.
        try:
            stored = chats.finish_reply(owner, chat_id, reply_sk, status, result)
        except Exception as exc:  # noqa: BLE001
            stored = False
            outcome = f"{outcome};store_error:{type(exc).__name__}"
    usage = data.get("usage") or {}
    _log(
        chat_id=chat_id,
        owner=owner,
        outcome=outcome,
        status=status,
        stored=stored,
        tools=[c.get("tool") for c in data.get("tool_calls") or []],
        grounded=result.get("grounded"),
        failure=result.get("failure"),
        retried=retried,
        stop_reason=data.get("stop_reason"),
        findings=len(result.get("findings") or []),
        citations=result.get("citations") or [],
        latency_ms=int((time.monotonic() - started) * 1000),
        input_tokens=usage.get("inputTokens"),
        output_tokens=usage.get("outputTokens"),
        cache_read_tokens=usage.get("cacheReadInputTokens"),
        cache_write_tokens=usage.get("cacheWriteInputTokens"),
        KbFallbacks=kb_fallbacks,
        _aws={"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [_KB_FALLBACK_METRIC]},
    )
    return {"ok": status == "complete"}
