"""Per-turn telemetry for the optimization-pillar tokenomics patterns.

The orchestrator can run two optimization patterns whose cost is otherwise
INVISIBLE because Strands discards the helper agent's result:

  * context management -- a summarizer agent compacts history; and
  * model routing -- a Nova "judge" agent classifies each step SIMPLE/COMPLEX.

Each `_record_*` function below is called once per turn (by `handle_turn`): it
folds that helper agent's spend into the `UsageCollector` (so per-agent costs
still sum to the turn total) and returns a small telemetry block describing what
the pattern did this turn (None when the pattern is OFF). Keeping this here keeps
`agent.py` focused on the turn flow rather than per-pattern measurement.
"""

from __future__ import annotations

import logging
from typing import Any

from strands import Agent

import config
from tokenomics.cost import UsageCollector
from tokenomics.factory import BuiltContextManager

logger = logging.getLogger(__name__)


def record_context_mgmt(
    ctx_mgr: BuiltContextManager,
    orchestrator: Agent,
    collector: UsageCollector,
    messages_restored: int,
    removed_before: int,
    blocks_masked: int = 0,
) -> dict[str, Any] | None:
    """Record the context-management pattern's effect for this turn (None when OFF).

    Adds the summarizer's own model calls to the collector: Strands drops the
    summarizer's AgentResult, so its spend never shows in the orchestrator's
    usage. Reads it off the summarizer's event-loop metrics (the summarizer is
    built fresh per turn, so that is exactly this turn's spend). The summarizer is
    an internal component of the orchestrator, not an agent: its cost is counted
    in the orchestrator's bucket (per-agent sums still equal the turn total) and
    broken out under `by_component.summarizer`.
    """
    if not ctx_mgr.enabled:
        return None
    summarized = False
    if ctx_mgr.summarizer is not None and (
        collector.record_side_agent(
            ctx_mgr.summarizer,
            ctx_mgr.summarizer_model_id,
            "orchestrator",
            component="summarizer",
        )
        > 0
    ):
        summarized = True
    return {
        "strategy": ctx_mgr.strategy,
        "messages_restored": messages_restored,
        "tool_blocks_masked": blocks_masked,
        "messages_after": len(orchestrator.messages),
        "messages_removed": max(0, ctx_mgr.manager.removed_message_count - removed_before),
        "summarized": summarized,
    }


def record_model_routing(flags, orch_router, orch_tracker, built, collector) -> dict[str, Any] | None:
    """Record the model-routing pattern's effect for this turn (None when OFF).

    Meters the orchestrator's Nova judge as its own `router_judge` agent bucket
    (Strands discards the judge's result, so its spend is otherwise invisible).
    The judge is a standalone agent called once per orchestrator model call; the
    strategy tallies each classify's own token delta as it goes, and this records
    that per-turn tally as the bucket -- rather than a single end-of-turn read of
    the judge's accumulated usage, which could mis-count depending on how the SDK
    accumulates across repeated calls. Also builds the routing telemetry block
    from the per-agent trackers.
    """
    if not flags.model_routing.enabled:
        return None

    p = flags.model_routing_params()
    cheap_id = p.cheap_model or config.KNOWLEDGE_MODEL
    capable_id = p.capable_model or config.ORCHESTRATOR_MODEL

    # Meter the judge (Nova) -- governed + metered, no free spend. The strategy
    # tallies each classify call's own tokens (per-call, stateless); record that
    # tally as the router_judge bucket. record_usage returns the cost, so there's
    # no fragile by_agent["router_judge"] re-lookup.
    judge_cost = 0.0
    strat = getattr(orch_router, "judge_strategy", None) if orch_router else None
    tally = getattr(strat, "usage", None) if strat else None
    if tally and (tally.get("inputTokens") or tally.get("outputTokens")):
        judge_cost = collector.record_usage(orch_router.judge_model_id, dict(tally), agent="router_judge")

    # Route distribution across every agent that carried a router this turn.
    routed_calls: dict[str, int] = {}
    for tracker in (orch_tracker, built.knowledge_tracker, built.order_tracker):
        if tracker is not None:
            for mid, n in tracker.model_calls.items():
                routed_calls[mid] = routed_calls.get(mid, 0) + n

    orch_cheap = orch_tracker.model_calls.get(cheap_id, 0) if orch_tracker else 0
    spec_escalated = sum(
        tracker.model_calls.get(capable_id, 0)
        for tracker in (built.knowledge_tracker, built.order_tracker)
        if tracker is not None
    )
    judge_calls = orch_router.judge_strategy.calls if (orch_router and orch_router.judge_strategy) else 0

    # Attribution is exact when each agent's invocation ran a single model, which
    # our (non-failure-aware) strategies guarantee -- they re-classify to the same
    # tier on a failed call, so routing ends rather than switching models. If a
    # future failure-aware strategy ever splits an invocation across models, cost
    # is still attributed to last_model (best-effort); surface it rather than hide it.
    for name, tracker in (
        ("orchestrator", orch_tracker),
        ("knowledge", built.knowledge_tracker),
        ("order", built.order_tracker),
    ):
        if tracker is not None and len(tracker.model_calls) > 1:
            logger.warning(
                "[routing] %s ran >1 model in one turn (%s); cost attributed to last_model "
                "(best-effort). Expected only with a failure-aware routing strategy.",
                name,
                dict(tracker.model_calls),
            )

    # Tier split by the CONFIGURED model ids (not a model-family substring), so
    # the benchmark distribution stays correct if the model pair is overridden.
    return {
        "enabled": True,
        "routed_calls": routed_calls,
        "model_calls": sum(routed_calls.values()),
        "capable_calls": routed_calls.get(capable_id, 0),
        "cheap_calls": routed_calls.get(cheap_id, 0),
        "orchestrator_cheap_calls": orch_cheap,
        "specialist_escalated_calls": spec_escalated,
        "judge_calls": judge_calls,
        "judge_cost_usd": round(judge_cost, 6),
    }
