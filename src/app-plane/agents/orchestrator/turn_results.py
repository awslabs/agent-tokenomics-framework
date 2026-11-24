"""Turn wrap-up: build the response payload, emit telemetry, advance counters.

The tail every `handle_turn` return path shares. Keeping it here means there is
exactly ONE place that assembles the per-turn usage, emits the `UsageRecorded`
event + the conditional governance event, builds the `TurnResult`, and advances
the cross-turn enforcement counters -- so the three return paths in `agent.py`
can't drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tokenomics import metering, quota_counter, session_counter
from tokenomics.cost import UsageCollector
from tokenomics.governance_outcome import GovernanceOutcome
from tokenomics.identity import RunContext


@dataclass
class TurnResult:
    """The outcome of one customer turn: the reply + tokenomics telemetry."""

    reply: str
    usage: dict[str, Any]
    identity: dict[str, str]
    model_ids: dict[str, str]
    # Token-governance enforcement outcome for this turn (empty => nothing acted).
    governance: dict[str, Any] = field(default_factory=dict)


def assemble_usage(collector: UsageCollector, ctx_stats: dict | None, mr_stats: dict | None) -> dict:
    """The turn's usage dict + the optional per-pattern telemetry blocks.

    Shared by both completed paths (normal + output-cap). `ctx_stats` is the
    context-management pattern's effect (None when OFF); `mr_stats` is the
    model-routing pattern's effect (None when OFF).
    """
    usage = collector.to_dict()
    if ctx_stats:
        usage["context_mgmt"] = ctx_stats
    if mr_stats:
        usage["model_routing"] = mr_stats
    return usage


def build_model_ids(built, mr_stats: dict | None, router) -> dict[str, str]:
    """The per-agent model ids for the response, + `router_judge` when routing ran."""
    model_ids = {
        "orchestrator": built.orchestrator_model_id,
        "knowledge": built.knowledge_model_id,
        "order": built.order_model_id,
    }
    if mr_stats and router.judge_model_id:
        model_ids["router_judge"] = router.judge_model_id
    return model_ids


def finalize_turn(
    ctx: RunContext,
    *,
    reply: str,
    usage: dict,
    model_ids: dict[str, str],
    outcome: GovernanceOutcome,
) -> TurnResult:
    """Emit the turn's telemetry and build its `TurnResult` -- the shared tail.

    Every return path ends here, so there is exactly ONE place that emits the
    per-turn `UsageRecorded` event, the conditional governance event, and builds
    the result. Emission is fire-and-forget (off the critical path; the reply is
    already composed) and never raises.
    """
    metering.emit_usage(
        user_id=ctx.user_id,
        session_id=ctx.session_id,
        run_id=ctx.run_id,
        flag_signature=ctx.flag_signature,
        usage=usage,
        governance=outcome.to_dict(),
        model_ids=model_ids,
    )
    # A separate typed governance event when an in-loop action acted (QuotaBlock /
    # ThrottleEvent / LimitViolation by reason), so the consumer rolls up the right
    # per-config / throttle counts + appends the event-log row.
    if outcome.acted():
        metering.emit_governance_event(
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            governance=outcome.to_dict(),
        )
    return TurnResult(
        reply=reply,
        usage=usage,
        identity=ctx.as_metadata(),
        model_ids=model_ids,
        governance=outcome.to_dict(),
    )


def turn_tokens(usage: dict) -> int:
    """The turn's full billable token total (input incl. cache + output)."""
    return int(usage.get("total_input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)


def advance_counters(ctx: RunContext, gov_config, session_cap: int, turn_total: int) -> None:
    """Advance the cross-turn enforcement counters after any turn that spent tokens.

    Best-effort (fire-and-forget): a dropped increment only slightly undercounts
    and the next read self-heals. Writes are gated so we never touch counters for
    a cap the user doesn't have (so benchmark / no-cap runs write nothing):
      * session counter -- only when a session cap is configured;
      * per-user DAY / MONTH / MIN -- only for the quota dimensions the user has.
    Includes cache tokens (the full billable total). Called on every path that
    spent tokens -- normal completion, the output-cap cutoff, and a turn that
    raised mid-way -- exactly once per turn. Only the pre-turn session-cap
    return (no model calls) skips it.
    """
    if turn_total <= 0:
        return
    if session_cap > 0:
        session_counter.add_session_tokens(ctx.session_id, turn_total)
    if ctx.user_id:
        user_quota = gov_config.quota_for(ctx.user_id)
        if user_quota.daily_max_tokens_per_user:
            quota_counter.add_day(ctx.user_id, turn_total)
        if user_quota.monthly_max_tokens_per_user:
            quota_counter.add_month(ctx.user_id, turn_total)
        if user_quota.max_tokens_per_minute_per_user:
            quota_counter.add_minute(ctx.user_id, turn_total)
