"""Build and run the Advisor agent for one turn.

Mirrors the app plane's construction (src/app-plane/tokenomics/factory.py):
a BedrockModel with prompt caching, plus a guardrail, a per-call output cap and
a reduced retry strategy; the agent is bounded by native `Limits(turns)` and a
wall-clock cancel signal, and sees the chat through a sliding window. Output is
structured: the narrative (figure references only), the figures to tabulate and
the catalog patterns applied.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any

from botocore.config import Config as BotoConfig
from pydantic import BaseModel, Field
from strands import Agent, ModelRetryStrategy
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig
from strands.types.exceptions import MaxTokensReachedException

from . import prompts, settings
from .tools import Recorder, invoke_tool, make_tools

logger = logging.getLogger(__name__)


class PatternUse(BaseModel):
    id: str = Field(description="Catalog pattern id, e.g. TKN-03, from search_patterns results")
    why: str = Field(description="One short clause: the observed signal it addresses (no numbers)")


class AdvisorAnswer(BaseModel):
    narrative: str = Field(
        description=f"The answer, at most {settings.NARRATIVE_MAX_WORDS} words, shaped as rule 13 "
        "says. Figures ONLY as [[tN.key]] references; never typed numbers."
    )
    table: list[str] = Field(
        default_factory=list,
        description="Figure references for a breakdown or comparison (rule 13), exactly as a tool "
        "listed them; shown as a table under the answer. Empty when the narrative "
        "alone answers.",
    )
    patterns_applied: list[PatternUse] = Field(
        default_factory=list, description="Catalog patterns this answer relies on"
    )
    title: str = Field(
        default="",
        description="First question of a chat only: a 2-5 word title for the "
        "conversation, no numbers or punctuation (e.g. 'Agent cost breakdown')",
    )


_index_lock = threading.Lock()
_index: str | None = None


def pattern_index(invoke=invoke_tool) -> str:
    """The compact catalog index, fetched once per container (static content).
    Only a successful fetch is cached, so a transient failure is retried next turn."""
    global _index
    with _index_lock:
        if _index:
            return _index
        res = invoke("get_catalog_index", {})
        text = str(res.get("index") or "") if isinstance(res, dict) else ""
        if text:
            _index = text
        return text


def build_model() -> BedrockModel:
    kwargs: dict[str, Any] = {
        "model_id": settings.ADVISOR_MODEL,
        "region_name": settings.AWS_REGION,
        "cache_config": CacheConfig(strategy="auto"),
        "max_tokens": settings.MAX_OUTPUT_TOKENS,
        "boto_client_config": BotoConfig(
            connect_timeout=2,
            read_timeout=settings.MODEL_READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": 1},
        ),
    }
    if settings.ADVISOR_GUARDRAIL_ID and settings.ADVISOR_GUARDRAIL_VERSION:
        kwargs.update(
            guardrail_id=settings.ADVISOR_GUARDRAIL_ID,
            guardrail_version=settings.ADVISOR_GUARDRAIL_VERSION,
            guardrail_latest_message=True,
            guardrail_trace="disabled",
        )
    return BedrockModel(**kwargs)


def _messages(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"role": h["role"], "content": [{"text": h["text"]}]} for h in history]


STRUCTURED_OUTPUT_PROMPT = "Please put your answer above into the answer form."


def _build_agent(history: list[dict[str, str]], recorder: Recorder, invoke, agent_cls) -> Any:
    """A fresh agent for this turn, seeded with the chat history trimmed to the window."""
    agent = agent_cls(
        model=build_model(),
        system_prompt=prompts.system_prompt(pattern_index(invoke)),
        tools=make_tools(recorder, invoke),
        messages=_messages(history),
        conversation_manager=SlidingWindowConversationManager(window_size=2 * settings.HISTORY_WINDOW_TURNS),
        callback_handler=None,
        retry_strategy=ModelRetryStrategy(max_attempts=3, initial_delay=2, max_delay=8),
        # Sent when the model replies in plain text; the default ("You must format ...") is
        # read by the prompt-attack guardrail as an injection and turns the reply into a decline.
        structured_output_prompt=STRUCTURED_OUTPUT_PROMPT,
    )
    # The SDK applies the window only after a turn (or on a context overflow) -- too late for an agent
    # rebuilt every turn -- so trim the seeded history now. Deliberately not per_turn=True: trimming
    # between the tool loop's model calls would shift the cached message prefix and turn reads into writes.
    agent.conversation_manager.apply_management(agent)
    return agent


def run_turn(
    message: str,
    history: list[dict[str, str]],
    window: str,
    user: str,
    invoke=invoke_tool,
    agent_cls=Agent,
    retry_note: str = "",
    deadline: float | None = None,
) -> dict[str, Any]:
    """One Advisor turn -> {outcome, narrative, patterns_applied, tool_calls, stop_reason, usage}
    (+ title and table when answered)."""
    recorder = Recorder()
    agent = _build_agent(history, recorder, invoke, agent_cls)
    cancel = threading.Event()
    timer = threading.Timer(deadline or settings.DEADLINE_SECONDS, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        result = agent(
            prompts.user_turn(message, window, user, retry_note),
            structured_output_model=AdvisorAnswer,
            limits={"turns": settings.MAX_TURNS},
            cancel_signal=cancel,
        )
    except Exception as exc:  # noqa: BLE001 -- includes structured-output failures
        # An answer form cut off at the output cap is never shown in part; the worker retries a
        # "max_tokens" turn asking for a shorter answer.
        stop = "max_tokens" if isinstance(exc, MaxTokensReachedException) else "error"
        logger.warning("[advisor-agent] turn ended without an answer (%s)\n%s", stop, traceback.format_exc())
        return {
            "outcome": "no_narrative",
            "narrative": "",
            "patterns_applied": [],
            "tool_calls": recorder.calls,
            "stop_reason": stop,
            "usage": {},
        }
    finally:
        timer.cancel()
    stop = str(getattr(result, "stop_reason", "") or "")
    usage = dict(getattr(getattr(result, "metrics", None), "accumulated_usage", {}) or {})
    base = {"tool_calls": recorder.calls, "stop_reason": stop, "usage": usage}
    if stop == "guardrail_intervened":
        return {"outcome": "declined", "narrative": "", "patterns_applied": [], **base}
    answer = getattr(result, "structured_output", None)
    if stop in ("cancelled", "limit_turns") or not isinstance(answer, AdvisorAnswer):
        return {
            "outcome": "timeout" if stop == "cancelled" else "no_narrative",
            "narrative": "",
            "patterns_applied": [],
            **base,
        }
    return {
        "outcome": "answered",
        "narrative": answer.narrative,
        "title": answer.title,
        "table": answer.table,
        "patterns_applied": [p.model_dump() for p in answer.patterns_applied],
        **base,
    }
