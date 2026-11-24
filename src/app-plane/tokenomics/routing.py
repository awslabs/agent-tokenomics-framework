"""Model-routing strategies (the `model_routing` pattern's routing *decision*).

Two Strands `RoutingStrategy` implementations, one per agent shape (design.md
§4). We supply only the decision; Strands' `ModelRouter` does candidate
resolution, the per-invocation state, and the failover budget ("reuse, never
duplicate").

- `HeuristicEscalateStrategy` (specialists): default Haiku, escalate *up* to
  Sonnet only on clear complexity. Deterministic -- no model call. Decline/error
  -> `None` -> Haiku default (fail cheap).
- `JudgeRouteDownStrategy` (orchestrator): default Sonnet, route *down* to Haiku
  when a one-shot Nova Lite judge says the step is simple. Decline/error ->
  `None` -> Sonnet default (fail safe).

A strategy returns a candidate **from `context.candidates`** (the router matches
by identity) or `None` to decline (the router then serves the first/default
candidate). Any exception is swallowed into a decline so a classifier hiccup
never breaks a turn -- the fail direction is set by which model is candidate 0.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterModelCallEvent, HookProvider, HookRegistry
from strands.models.routing import RoutingCandidate, RoutingContext

logger = logging.getLogger(__name__)


# ---- step-text extraction --------------------------------------------------


def _latest_user_text(context: RoutingContext) -> str:
    """The text of the most recent user turn (what the step is really about).

    SDK guidance: look at the latest turn, not the whole (cached, stable) history
    -- the system prompt doesn't determine a step's complexity. Returns "" when
    no user text is present (-> treated as not-complex / decline).
    """
    messages = getattr(context, "messages", None) or []
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        parts = [
            b.get("text", "") for b in (msg.get("content") or []) if isinstance(b, dict) and b.get("text")
        ]
        text = " ".join(p for p in parts if p).strip()
        if text:
            return text
    return ""


def _candidate_by_name(context: RoutingContext, name: str) -> RoutingCandidate | None:
    """The candidate instance with this name (router matches by identity)."""
    for c in getattr(context, "candidates", None) or []:
        if c.name == name:
            return c
    return None


# ---- heuristic (specialists): escalate UP on clear complexity --------------

# Escalate ONLY on an explicit reasoning signal. A specialist classifies the
# orchestrator's composed TASK STRING, which routinely carries order ids, dates,
# and dollar amounts even for trivial lookups (e.g. "status of ORD-1001 placed
# 2026-10-02 for $129.99"). Bare-number / order-id-count / length rules therefore
# over-escalate routine work to the capable model and erode the pattern's savings,
# so they are deliberately NOT used. A genuine cross-referencing task ("compare
# ORD-1001 and ORD-1002") carries a reasoning keyword and is caught below. When
# in doubt, fail cheap (stay on Haiku) -- the accepted V1 specialist tradeoff.
REASONING_KEYWORDS = (
    "why",
    "compare",
    "comparison",
    "versus",
    " vs ",
    "explain",
    "reconcile",
    "difference",
    "differ",
    "trade-off",
    "tradeoff",
    "step by step",
    "step-by-step",
    "analyze",
    "analyse",
    "evaluate",
    "pros and cons",
)


class HeuristicEscalateStrategy:
    """Specialist strategy: Haiku default, escalate to the capable model only on a
    clear reasoning signal.

    Deterministic (no model call). `capable_name` must match the capable
    candidate's `name` in the router.
    """

    def __init__(self, capable_name: str) -> None:
        self._capable_name = capable_name

    async def select(self, context: RoutingContext, **_: Any) -> RoutingCandidate | None:
        try:
            if self._is_complex(context):
                return _candidate_by_name(context, self._capable_name)  # -> Sonnet
            return None  # simple / uncertain -> decline -> Haiku default
        except Exception as exc:  # noqa: BLE001 -- fail cheap (Haiku) on any hiccup
            logger.warning("[routing] heuristic classify failed, staying on cheap default: %r", exc)
            return None

    def _is_complex(self, context: RoutingContext) -> bool:
        text = _latest_user_text(context)
        if not text:
            return False
        return any(kw in text.lower() for kw in REASONING_KEYWORDS)


# ---- LLM-as-judge (orchestrator): route DOWN when the step is simple -------

_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")

_JUDGE_INSTRUCTIONS = (
    "You are a routing classifier. Decide if the following assistant step is "
    "SIMPLE (a trivial lookup, acknowledgement, formatting, or single-fact reply "
    "a small model handles well) or COMPLEX (multi-step reasoning, planning, "
    "weighing trade-offs, or synthesizing several facts). "
    "Answer with exactly one word: SIMPLE or COMPLEX."
)


class JudgeRouteDownStrategy:
    """Orchestrator strategy: Sonnet default, route down to the cheap model when the judge says simple.

    `judge_agent` is a small Strands Agent (Nova Lite) built by the factory with
    the shared quota hook + a low output cap. We make one classify call over the
    latest step; "simple" -> cheap candidate, anything else / error -> decline
    (-> Sonnet default, fail safe).
    """

    def __init__(self, cheap_name: str, judge_agent: Any) -> None:
        self._cheap_name = cheap_name
        self._judge = judge_agent
        self.calls = 0  # number of judge classify calls this turn (telemetry)
        # Per-call usage tally for the turn. The judge is a standalone Agent that
        # may be called more than once per turn; we accumulate each classify's OWN
        # tokens here (a fresh delta per call) and the runner meters this tally as
        # the `router_judge` bucket -- not a single end-of-turn read, which would
        # over/under-count depending on the agent's accumulation behaviour.
        self.usage: dict[str, int] = dict.fromkeys(_USAGE_KEYS, 0)

    def _accumulated(self) -> dict[str, int]:
        m = getattr(self._judge, "event_loop_metrics", None)
        a = getattr(m, "accumulated_usage", None) or {}
        return {k: int(a.get(k, 0) or 0) for k in _USAGE_KEYS}

    async def select(self, context: RoutingContext, **_: Any) -> RoutingCandidate | None:
        try:
            if self._classify(context) == "simple":
                return _candidate_by_name(context, self._cheap_name)  # -> Haiku
            return None  # complex / uncertain -> decline -> Sonnet default
        except Exception as exc:  # noqa: BLE001 -- fail safe (Sonnet) on any judge error
            # Logged, not silent: a judge IAM/throttle failure degrades to the
            # expensive default, which otherwise looks like "routing never helps".
            logger.warning("[routing] judge classify failed, staying on capable default: %r", exc)
            return None

    def _classify(self, context: RoutingContext) -> str:
        task = _latest_user_text(context)
        if not task:
            return "complex"  # nothing to judge -> keep the capable default
        prompt = f"{_JUDGE_INSTRUCTIONS}\n\nStep:\n{task}\n\nAnswer:"
        # Stateless per classify: clear any prior exchange so this call sees only
        # the current step (prior classifies would inflate its input), then tally
        # THIS call's tokens as the delta of the judge's accumulated usage.
        try:
            self._judge.messages = []
        except Exception:  # noqa: BLE001 -- stub judges may not have .messages
            pass
        before = self._accumulated()
        self.calls += 1
        result = self._judge(prompt)
        after = self._accumulated()
        for k in _USAGE_KEYS:
            self.usage[k] += max(0, after[k] - before[k])
        text = str(result).strip().lower()
        # Robust parse: explicit "simple" wins; default to complex (fail safe).
        return "simple" if "simple" in text and "complex" not in text else "complex"


# ---- cost attribution: which model each agent actually ran on --------------

# Strands stores a router's per-invocation selection in `invocation_state` under
# a key with this prefix (verified against strands-agents 1.61.0). The design's
# original plan (read model + per-call usage off AfterModelCallEvent) doesn't
# hold on 1.61.0: the event carries no usage and accumulated_usage reads zero at
# hook time. The selected model IS reachable here, and routing selects once per
# invocation (switching only on a failed call), so pricing an agent's end-of-run
# usage at the selected model is correct for the normal case.
_ROUTING_STATE_PREFIX = "strands:model_routing"


def selected_model_id(invocation_state: dict | None) -> str | None:
    """The model id a router selected for this call, from the routing state.

    Returns None when routing isn't active for this agent (no routing state).
    """
    for key, state in (invocation_state or {}).items():
        if not isinstance(key, str) or not key.startswith(_ROUTING_STATE_PREFIX):
            continue
        model = getattr(state, "model", None)
        if model is None:
            continue
        try:
            cfg = model.get_config() if hasattr(model, "get_config") else getattr(model, "config", {})
            mid = cfg.get("model_id") if isinstance(cfg, dict) else None
        except Exception:  # noqa: BLE001 -- never break a turn over attribution
            mid = None
        if mid:
            return str(mid)
    return None


class RoutingUsageTracker(HookProvider):
    """Capture the model an agent actually ran on under routing (per turn).

    Attached to a routed agent, it records the selected model on every model call
    (`AfterModelCallEvent`). The runner then prices that agent's end-of-run
    `accumulated_usage` at `last_model` instead of the agent's static default, so
    a routed agent's cost is attributed to the model that really ran.

    `model_calls` (model_id -> count) feeds the per-turn routing telemetry block.
    `last_model` is "" until the first routed call, so callers fall back to the
    static model id (OFF path / nothing routed).
    """

    def __init__(self) -> None:
        self.last_model: str = ""
        self.model_calls: dict[str, int] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self._after)

    def _after(self, event: AfterModelCallEvent) -> None:
        mid = selected_model_id(getattr(event, "invocation_state", None))
        if mid:
            self.last_model = mid
            self.model_calls[mid] = self.model_calls.get(mid, 0) + 1
