"""Advisor tools: thin @tool wrappers over the in-process data tools (advisor/data_tools.py).

Each call runs the read-only data tools with a per-call time limit, and the full
result is recorded per invocation (never in module globals -- containers are
reused across requests). The model sees only figure *references* and display
strings, so it cites `[[t1.key]]` instead of typing numbers.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from strands import tool

from advisor import data_tools as advisor_tools
from advisor import service as svc

from . import settings

_TOOL_FAILED = {"error": "tool failed; try again or ask a different question"}
_MAX_IN_FLIGHT = 10
# Bounds each call; a timed-out call finishes in the background and its result is dropped.
_pool = ThreadPoolExecutor(max_workers=_MAX_IN_FLIGHT, thread_name_prefix="advisor-tool")
# One slot per running call, freed only when the call really ends (not when the caller stops
# waiting), so timed-out calls can't pile up a queue: when every slot is busy, new calls are
# rejected at once rather than waiting behind them.
_slots = threading.BoundedSemaphore(_MAX_IN_FLIGHT)


logger = logging.getLogger(__name__)


def invoke_tool(tool_name: str, args: dict[str, Any], turn: svc.TurnContext | None = None) -> dict[str, Any]:
    """Run one data tool in-process. The JSON round-trip keeps results identical to a
    serialised payload (tuples become lists; anything non-JSON fails safely)."""
    if not _slots.acquire(blocking=False):
        logger.warning("[advisor-tools] %s rejected: all tool slots are busy", tool_name)
        return dict(_TOOL_FAILED)
    try:
        future = _pool.submit(advisor_tools.dispatch, tool_name, args, turn)
    except RuntimeError:  # the pool is shutting down: give the slot back
        _slots.release()
        return dict(_TOOL_FAILED)
    future.add_done_callback(lambda _: _slots.release())
    try:
        data = json.loads(json.dumps(future.result(timeout=settings.TOOL_TIMEOUT_SECONDS)))
    except FutureTimeout:
        logger.warning("[advisor-tools] %s timed out", tool_name)
        return dict(_TOOL_FAILED)
    except (TypeError, ValueError):
        logger.warning("[advisor-tools] %s returned a non-JSON result", tool_name)
        return dict(_TOOL_FAILED)
    return data if isinstance(data, dict) else {"error": "tool returned an invalid result"}


class Recorder:
    """Per-invocation record of every tool call (the grounding evidence)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.shown_patterns: set[str] = set()  # full pattern text is shown once per turn
        self._lock = threading.Lock()

    def record(self, tool_name: str, result: dict[str, Any]) -> str:
        with self._lock:
            ref = f"t{len(self.calls) + 1}"
            self.calls.append({"ref": ref, "tool": tool_name, "result": result})
            return ref


def _pattern_view(p: dict[str, Any], shown: set[str]) -> dict[str, Any]:
    pid = p.get("pattern_id")
    if pid in shown:
        return {"id": pid, "name": p.get("name")}
    shown.add(pid)
    return {
        "id": pid,
        "name": p.get("name"),
        "summary": p.get("summary"),
        "recommendation": p.get("recommendation"),
        "expected_effect": p.get("expected_effect"),
        "quality_risk": p.get("quality_risk_text") or p.get("quality_risk"),
        "threshold": p.get("threshold"),
    }


def model_view(ref: str, result: dict[str, Any], shown: set[str]) -> dict[str, Any]:
    """What the model sees: references + display strings, never raw values. `shown` holds the
    pattern ids already shown in full this turn (updated in place)."""
    if "error" in result:
        out = {"error": result["error"]}
        if result.get("candidates"):
            out["candidates"] = result["candidates"]
        return out
    view: dict[str, Any] = {
        "available": result.get("available", True),
        "data_level": result.get("data_level"),
        "figures": {
            f"[[{ref}.{k}]]": {"shows": f.get("display"), "signal": f.get("signal"), "level": f.get("level")}
            for k, f in (result.get("figures") or {}).items()
        },
    }
    for k in ("note", "reason"):
        if result.get(k):
            view[k] = result[k]
    if result.get("labels"):
        # Scalar text labels are citable too, e.g. [[t3.first_drop_day]] for a date.
        view["labels"] = {
            (f"[[{ref}.{k}]]" if isinstance(v, str) else k): v for k, v in result["labels"].items()
        }
    if result.get("signals"):
        # What the tool detected, with the catalog patterns that match it.
        view["signals"] = [
            {
                "signal": s.get("signal"),
                "level": s.get("level"),
                "figures": [f"[[{ref}.{k}]]" for k in s.get("figures") or []],
                "patterns": [_pattern_view(p, shown) for p in s.get("patterns") or [] if isinstance(p, dict)],
            }
            for s in result["signals"]
        ]
    if result.get("patterns") is not None:
        view["patterns"] = [_pattern_view(p, shown) for p in result["patterns"]]
    return view


def make_tools(recorder: Recorder, invoke: Callable[..., dict[str, Any]] = invoke_tool) -> list:
    """Build this invocation's tools, bound to its recorder and to one per-turn data context."""
    turn = svc.TurnContext()

    def run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        window = args.get("window")
        if window is not None and window not in settings.WINDOWS:
            return {"error": "window must be 24h, 7d or 30d"}
        try:
            result = invoke(name, {k: v for k, v in args.items() if v is not None}, turn)
        except Exception:  # noqa: BLE001 -- a tool failure must not crash the turn
            logger.warning("[advisor-agent] tool %s failed\n%s", name, traceback.format_exc())
            result = {"error": "tool failed; try again or ask a different question"}
        ref = recorder.record(name, result)
        with recorder._lock:
            return model_view(ref, result, recorder.shown_patterns)

    @tool
    def get_cost_summary(window: str = "7d", user: str = "all") -> dict:
        """Total cost/tokens, KPIs, cost per session/turn, per-agent cost with model, share of spend,
        cost per 1k tokens and output share, per-model cost, and top users vs the median user.

        Args:
            window: 24h, 7d or 30d.
            user: "all", a user's email, or a user's name.
        """
        return run("get_cost_summary", {"window": window, "user": user})

    @tool
    def get_period_comparison(window: str = "7d", user: str = "all") -> dict:
        """This period vs the previous equal-length period: total, per-agent and per-model cost with
        absolute/% change and trend, and cache hit rate in both periods.

        Args:
            window: 24h, 7d or 30d.
            user: "all", a user's email, or a user's name.
        """
        return run("get_period_comparison", {"window": window, "user": user})

    @tool
    def get_cache_health(window: str = "7d", agent_name: str = "") -> dict:
        """Prompt-cache hit rate, cache read:write, input share of tokens, by-day hit rate, the first
        day it dropped, and whether the prompt_cache flag is on.

        Args:
            window: 24h, 7d or 30d.
            agent_name: optional agent name (orchestrator, knowledge, order); empty for the whole fleet.
        """
        # (`agent` is reserved by Strands for injecting the Agent instance.)
        return run("get_cache_health", {"window": window, "agent_name": agent_name or None})

    @tool
    def get_outliers(window: str = "7d") -> dict:
        """Users and agents whose daily spend is more than 10x their peer median, and sessions whose
        tokens per turn are more than 5x the peer average.

        Args:
            window: 24h, 7d or 30d.
        """
        return run("get_outliers", {"window": window})

    @tool
    def get_user_profile(user: str, window: str = "7d") -> dict:
        """One user's sessions, turns, tokens per turn, turns per session, cost per session, agent
        share and cache hit rate, each next to the median user, plus the active optimization patterns.

        Args:
            user: a user's email or name.
            window: 24h, 7d or 30d.
        """
        return run("get_user_profile", {"window": window, "user": user})

    @tool
    def get_plan_vs_actual(window: str = "7d", user: str = "all") -> dict:
        """Usage against the window-matched per-user cap, users near quota, cap violations, quota
        blocks and throttles.

        Args:
            window: 24h, 7d or 30d.
            user: "all", a user's email, or a user's name.
        """
        return run("get_plan_vs_actual", {"window": window, "user": user})

    @tool
    def get_governance_config() -> dict:
        """The current governance caps per agent, per session and per user (0 means no cap)."""
        return run("get_governance_config", {})

    @tool
    def get_turn_curve(session_id: str) -> dict:
        """Turn-over-turn tokens and cost for one session, the spike turn, model calls per agent
        and governance outcomes.

        Args:
            session_id: the session id (letters, digits, '-' or '_').
        """
        return run("get_turn_curve", {"session_id": session_id})

    @tool
    def search_patterns(query: str, k: int = 3) -> dict:
        """Semantic search of the tokenomics pattern catalog. Query with one observed signal,
        e.g. "cache hit rate low" or "one model dominates spend". Returns the closest patterns with
        their recommendation, expected effect and quality risk.

        Args:
            query: the observed signal to match (3-500 characters).
            k: number of patterns to return (1-5).
        """
        return run("search_patterns", {"query": query, "k": k})

    return [
        get_cost_summary,
        get_period_comparison,
        get_cache_health,
        get_outliers,
        get_user_profile,
        get_plan_vs_actual,
        get_governance_config,
        get_turn_curve,
        search_patterns,
    ]
