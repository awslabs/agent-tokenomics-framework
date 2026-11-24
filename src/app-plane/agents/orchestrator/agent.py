"""Orchestrator runner -- executes one customer turn end to end.

Flow:
  1. Read the live flag config (or a benchmark's pinned override).
  2. Build the three agents via the flag-aware factory (prompt-cache + context
     management applied here).
  3. Wire the specialists in as orchestrator tools (in-process delegation).
  4. Run the orchestrator on the customer's message.
  5. Record token usage/cost across orchestrator + specialists.
  6. Return the reply plus the tokenomics telemetry for this turn.

`handle_turn` is the single entry point the AgentCore Runtime entrypoint
(`entrypoint.py`) calls per turn.
"""

from __future__ import annotations

import contextlib
import logging

from delegation import make_specialist_tools
from strands import Agent
from strands.types.exceptions import MaxTokensReachedException

import config
from agents.orchestrator.pattern_telemetry import record_context_mgmt, record_model_routing
from agents.orchestrator.prompts import ORCHESTRATOR_PROMPT
from agents.orchestrator.turn_results import (
    TurnResult,
    advance_counters,
    assemble_usage,
    build_model_ids,
    finalize_turn,
    turn_tokens,
)
from tokenomics import flags as flags_mod
from tokenomics import plans as plans_mod
from tokenomics import session_counter, turn_context
from tokenomics.cost import UsageCollector
from tokenomics.factory import (
    build_conversation_manager,
    build_model,
    build_router,
    build_specialists,
    mask_restored_tool_history,
)
from tokenomics.governance_outcome import GovernanceOutcome
from tokenomics.hooks.quota import QuotaHook
from tokenomics.hooks.tool_cap import ToolCapHook
from tokenomics.identity import RunContext
from tokenomics.routing import RoutingUsageTracker

logger = logging.getLogger(__name__)


def _memory_session_manager(ctx: RunContext, flags):
    """Build an AgentCore short-term memory session manager for this turn.

    Returns None (⇒ stateless orchestrator, today's behavior) when:
      - the `memory` flag is OFF, or
      - no memory resource is configured (AGENTCORE_MEMORY_ID empty), or
      - the memory integration can't be imported / constructed.

    Memory is scoped by (actor_id, session_id):
      - actor_id  = ctx.user_id  (Cognito sub for live chat; synthetic in benchmarks)
      - session_id = ctx.session_id ({sub}-{conv} live; {run_id}-{index} in benchmarks)
    so live conversations recall their own turns and each benchmark run stays
    isolated (unique session_id per run) -- no cross-run bleed.
    """
    if not flags.memory.enabled or not config.AGENTCORE_MEMORY_ID:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        params = flags.memory_params()
        cfg = AgentCoreMemoryConfig(
            memory_id=config.AGENTCORE_MEMORY_ID,
            actor_id=ctx.user_id,
            session_id=ctx.session_id,
            batch_size=params.batch_size,
        )
        return AgentCoreMemorySessionManager(cfg, region_name=config.AWS_REGION)
    except Exception as exc:  # noqa: BLE001 -- never let memory break a turn
        logger.warning("[memory] disabled for this turn (%s: %s)", type(exc).__name__, exc)
        return None


def _partial_text(agent: Agent) -> str:
    """Best-effort: pull the last assistant text from the agent's messages.

    When a per-call output cap trips, Strands raises MaxTokensReachedException but
    first appends the partial assistant message to `agent.messages`. We salvage
    that text so the customer still sees whatever was generated before the cutoff.
    """
    with contextlib.suppress(Exception):  # salvage is best-effort
        for msg in reversed(getattr(agent, "messages", []) or []):
            if msg.get("role") != "assistant":
                continue
            parts = [
                b.get("text", "") for b in (msg.get("content") or []) if isinstance(b, dict) and b.get("text")
            ]
            text = " ".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def _settle_failed_turn(
    ctx: RunContext,
    *,
    gov_config,
    session_cap: int,
    collector: UsageCollector,
    orchestrator: Agent | None,
    orch_model_id: str,
    routing: tuple,
    outcome: GovernanceOutcome,
) -> None:
    """Count the spend of a turn that raised after model calls had started.

    Advances the session/quota counters and emits the usage event for whatever
    was already spent (specialists record into `collector` as they finish; the
    orchestrator's own usage is recovered from its event-loop metrics when
    `orchestrator` is given, i.e. it was not recorded before the failure).
    Best-effort and never raises, so the caller's original error is preserved.
    """
    try:
        if orchestrator is not None:
            collector.record_side_agent(orchestrator, orch_model_id, "orchestrator")
        flags, orch_router, orch_tracker, built = routing
        mr_stats = None
        with contextlib.suppress(Exception):  # judge spend is a best-effort extra
            mr_stats = record_model_routing(flags, orch_router, orch_tracker, built, collector)
        usage = assemble_usage(collector, None, mr_stats)
        advance_counters(ctx, gov_config, session_cap, turn_tokens(usage))
        finalize_turn(
            ctx,
            reply="",
            usage=usage,
            model_ids=build_model_ids(built, mr_stats, orch_router),
            outcome=outcome,
        )
    except Exception:  # never mask the turn's real error
        logger.exception("[turn] could not record spend for a failed turn")


def handle_turn(message: str, ctx: RunContext) -> TurnResult:
    """Run one customer turn and return the reply plus usage/cost telemetry."""
    flags = flags_mod.current()
    ctx.flag_signature = flags.flag_signature()

    # Token-governance: read the config (soft, in-loop caps). Empty => no caps.
    gov_config = plans_mod.current()
    orch_limits_cfg = gov_config.agent_limits("orchestrator")
    outcome = GovernanceOutcome()

    # Make the signed-in user's identity + profile available to the tools
    # (lookup_customer reads the profile; get_orders reads user_id).
    turn_context.set_current(ctx.user_id, ctx.profile)

    # Pre-turn per-session cap (soft): if this conversation has already spent past
    # session_max_tokens, stop before spending more. Reads the app-side session
    # counter (best-effort; a store hiccup just skips the check -> baseline).
    session_cap = int(gov_config.session_max_tokens or 0)
    if session_cap > 0:
        spent = session_counter.session_tokens(ctx.session_id)
        if spent >= session_cap:
            outcome.record(
                stopped=True,
                limited=False,
                reason="session_budget",
                scope="session",
                detail=f"session reached {session_cap:,} tokens",
                config="session_max_tokens",
            )
            # Stop before spending: empty usage, no model calls. The session-cap
            # enforcement shows in the Limit-violations widget + event log.
            return finalize_turn(
                ctx,
                reply="This conversation has reached its token limit. Please start a new conversation.",
                usage=UsageCollector().to_dict(),
                model_ids={},
                outcome=outcome,
            )

    # Per-user daily/monthly quota + TPM throttle (in-loop, ADR-001). ONE shared
    # hook per turn, keyed on this user, attached to EVERY agent via the factory
    # so no model call escapes it. Empty config (benchmark) => no caps => no-op.
    quota_hook = QuotaHook(ctx.user_id, gov_config.quota_for(ctx.user_id), outcome)

    built = build_specialists(flags, gov_config, hooks=[quota_hook], outcome=outcome)
    collector = UsageCollector()

    # Wire specialists as in-process delegation tools for this turn. Each carries
    # its own per-agent Limits; a specialist cutoff is recorded on `outcome`.
    specialist_tools = make_specialist_tools(
        knowledge=built.knowledge,
        order=built.order,
        knowledge_model_id=built.knowledge_model_id,
        order_model_id=built.order_model_id,
        collector=collector,
        plans=gov_config,
        outcome=outcome,
        knowledge_tracker=built.knowledge_tracker,
        order_tracker=built.order_tracker,
    )

    # Governance hooks: the shared per-user quota/throttle hook (pre-call money
    # gate, attached to every agent) + the per-turn tool-call cap (the specialists
    # get theirs in build_specialists).
    hooks = [quota_hook]
    if orch_limits_cfg.tool_calls_limit():
        hooks.append(ToolCapHook(orch_limits_cfg.tool_calls_limit(), outcome, agent_name="orchestrator"))

    # Model-routing cost attribution: when ON, the orchestrator carries a usage
    # tracker so its turn is priced at the model routing actually ran (route-down
    # to Haiku on simple steps), not the static Sonnet default. None when OFF.
    orch_tracker = RoutingUsageTracker() if flags.model_routing.enabled else None
    if orch_tracker is not None:
        hooks.append(orch_tracker)

    # Build the orchestrator WITH the specialist tools (same flag-aware model).
    # When the `memory` pattern is on (and a memory resource is configured), attach
    # a short-term memory session manager so the orchestrator recalls prior turns
    # in this conversation. None => stateless, exactly as before.
    session_manager = _memory_session_manager(ctx, flags)
    # Context management pattern: Strands' native conversation manager keeps the
    # orchestrator's (memory-restored) history lean. A manager is attached on
    # EVERY turn (OFF => Strands-default sliding window) so the class restoring a
    # session's persisted state never silently changes between turns.
    # Model routing pattern: when ON, the orchestrator runs behind a Strands
    # ModelRouter (Sonnet default, route DOWN to Haiku when the LLM judge says the
    # step is simple). OFF -> the static Sonnet model (baseline). The static model
    # is still built either way to size the context manager's window (routing is
    # among same-window Claude models, so this stays correct).
    orch_model = build_model(config.ORCHESTRATOR_MODEL, flags, orch_limits_cfg, guardrail=True)
    # The judge gets its OWN QuotaHook instance (same user / quota / shared outcome),
    # so it is still gated and recorded (ADR-001), but its tiny per-step projection
    # no longer memoizes the turn's quota decision for the orchestrator. The
    # orchestrator's shared `quota_hook` then decides with its own (real) projection,
    # keeping the per-minute "block the crossing turn" check accurate.
    judge_quota_hook = QuotaHook(ctx.user_id, gov_config.quota_for(ctx.user_id), outcome)
    orch_router = build_router("orchestrator", flags, orch_limits_cfg, quota_hook=judge_quota_hook)
    ctx_mgr = build_conversation_manager(flags, orch_model, hooks=[quota_hook])
    orchestrator = Agent(
        model=orch_router.router or orch_model,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=specialist_tools,
        callback_handler=None,
        session_manager=session_manager,
        hooks=hooks,
        # Stable agent id so AgentCore Memory restores prior turns on the NEXT
        # invoke: the runtime rebuilds the agent per turn, and the session
        # manager keys restored history by (session_id, agent_id). A random id
        # (Strands default) would look like a brand-new agent each turn and skip
        # restore. Only set it when memory is active so nothing else changes.
        **({"agent_id": "orchestrator"} if session_manager else {}),
        conversation_manager=ctx_mgr.manager,
    )
    # Observation masking (primary compaction): mask prior turns' tool traffic in
    # the restored history before this turn runs. Count-preserving, so the
    # conversation manager's restore offset stays aligned.
    blocks_masked = (
        mask_restored_tool_history(orchestrator.messages)
        if session_manager and ctx_mgr.mask_tool_history
        else 0
    )
    # Snapshot after memory restore so the turn's trim/summarize can be reported.
    messages_restored = len(orchestrator.messages)
    removed_before = ctx_mgr.manager.removed_message_count

    # Per-turn caps via native Strands Limits (iteration cutoff F1.2 + token budget
    # F1.1). The loop stops gracefully with a limit_* stop_reason (no exception).
    orch_limits = orch_limits_cfg.as_strands_limits()
    orch_recorded = False
    try:
        result = orchestrator(message, **({"limits": orch_limits} if orch_limits else {}))
        orch_model_id = (
            orch_tracker.last_model
            if orch_tracker and orch_tracker.last_model
            else built.orchestrator_model_id
        )
        collector.record_result(orch_model_id, result, agent="orchestrator")
        orch_recorded = True
        # Interpret the orchestrator's stop_reason for native-Limits / max_tokens cutoffs.
        outcome.note_stop_reason(getattr(result, "stop_reason", None))
    except MaxTokensReachedException:
        # The per-model-call output cap (max_output_tokens_per_model_call) cut the
        # model off mid-generation. Strands RAISES here (doesn't return a partial
        # result), so treat it as a graceful output-cap cutoff rather than a hard
        # error: record the governance outcome + return the partial text we have.
        outcome.record(
            stopped=True,
            limited=True,
            reason="limit_output_tokens",
            scope="turn",
            detail="model hit the per-call output-token cap",
            config="max_output_tokens_per_model_call",
        )
        # The exception fires BEFORE collector.record_result, so the tokens that
        # were actually spent on this (truncated) call aren't recorded yet -> cost
        # would wrongly show $0. Recover them from the agent's event-loop metrics
        # (Strands accumulates usage there even on a max-tokens stop).
        orch_model_id = (
            orch_tracker.last_model
            if orch_tracker and orch_tracker.last_model
            else built.orchestrator_model_id
        )
        collector.record_side_agent(orchestrator, orch_model_id, "orchestrator")
        partial = _partial_text(orchestrator)
        ctx_stats = record_context_mgmt(
            ctx_mgr, orchestrator, collector, messages_restored, removed_before, blocks_masked
        )
        mr_stats = record_model_routing(flags, orch_router, orch_tracker, built, collector)
        usage = assemble_usage(collector, ctx_stats, mr_stats)
        model_ids = build_model_ids(built, mr_stats, orch_router)
        # Only show salvaged text if it looks like a real answer — skip Strands'
        # internal tool-use/limit notes which aren't meaningful to a customer.
        clean = (
            partial
            if partial and "tool use" not in partial.lower() and "token limit" not in partial.lower()
            else ""
        )
        # The truncated turn is still billed, so it MUST advance the session/quota
        # counters like a completed turn -- otherwise a customer who keeps asking
        # for answers long enough to hit the output cap never moves toward any limit.
        advance_counters(ctx, gov_config, session_cap, turn_tokens(usage))
        return finalize_turn(
            ctx,
            reply=clean
            or "The response was cut short by the per-response token limit. "
            "Please ask a more specific question, or an operator can raise the output cap.",
            usage=usage,
            model_ids=model_ids,
            outcome=outcome,
        )
    except Exception:
        # Any other failure (Bedrock error, tool error, timeout) can land AFTER
        # orchestrator / specialist model calls were already paid for. Count that
        # spend before the error propagates to the entrypoint's generic reply.
        _settle_failed_turn(
            ctx,
            gov_config=gov_config,
            session_cap=session_cap,
            collector=collector,
            orchestrator=None if orch_recorded else orchestrator,
            orch_model_id=(
                orch_tracker.last_model
                if orch_tracker and orch_tracker.last_model
                else built.orchestrator_model_id
            ),
            routing=(flags, orch_router, orch_tracker, built),
            outcome=outcome,
        )
        raise

    ctx_stats = record_context_mgmt(
        ctx_mgr, orchestrator, collector, messages_restored, removed_before, blocks_masked
    )
    mr_stats = record_model_routing(flags, orch_router, orch_tracker, built, collector)
    usage = assemble_usage(collector, ctx_stats, mr_stats)
    model_ids = build_model_ids(built, mr_stats, orch_router)

    # Advance the running counters the NEXT turn reads (session cap + per-user
    # quota/throttle). Every path that spent tokens does this: here, the
    # output-cap branch above, and _settle_failed_turn. Only the pre-turn
    # session-cap return (no model calls) skips it.
    advance_counters(ctx, gov_config, session_cap, turn_tokens(usage))

    # A soft cap (iteration / tool-call / agent-token) can stop the orchestrator
    # BEFORE it writes a final answer -> str(result) is empty. Give the customer a
    # clear fallback so they don't see a blank bubble; the specific limit is named
    # in the blue info banner (SPA reads governance.reason).
    reply_text = str(result)
    if not reply_text.strip() and outcome.acted():
        reply_text = (
            "I couldn't finish composing a full answer within the current usage limits. "
            "Please try a more specific question, or an operator can raise the limit."
        )

    return finalize_turn(ctx, reply=reply_text, usage=usage, model_ids=model_ids, outcome=outcome)
