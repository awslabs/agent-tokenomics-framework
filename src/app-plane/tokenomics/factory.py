"""Flag-aware agent factory.

Builds the three agents (orchestrator + knowledge + order) with the tokenomics
patterns applied according to the live flag config. V1 patterns: prompt-cache
placement and context management (the orchestrator's conversation manager).

Design note (verified against Strands 1.54.0):
  Strands applies Bedrock prompt caching natively via BedrockModel's
  `cache_config` (modern, non-deprecated) -- it places the cachePoint blocks
  after the stable prefix (tools -> system) for us. So "prompt-cache placement"
  in V1 is implemented by BUILDING the model with `CacheConfig(strategy="auto")`
  when the flag is ON, and WITHOUT it when OFF. This honors the project's
  guiding rule ("reuse, never duplicate" -- don't hand-roll what the SDK ships).

  Consequence for the toggle tier: because caching is set at model construction,
  the flag applies when the factory next builds agents (i.e. next session/turn),
  not mid-stream within an in-flight model call. For the benchmark this is exactly
  right -- each conversation is built fresh under one pinned flag config.

  Baseline (flag OFF): no cache_config -> byte-identical requests, just uncached
  -> cacheRead/Write tokens are 0. Optimized (flag ON): cache_config -> the stable
  prefix is cached -> cacheReadInputTokens climbs on repeat calls. That is the
  clean, honest before/after signal, with no change to model output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from strands import Agent
from strands.agent.conversation_manager import (
    ConversationManager,
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)
from strands.hooks import HookProvider
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig
from strands.models.routing import ModelRouter, RoutingCandidate
from tools import get_orders, lookup_customer, search_kb

import config
from agents.knowledge.prompts import KNOWLEDGE_PROMPT
from agents.order.prompts import ORDER_PROMPT
from shared.flags_schema import CTX_SUMMARIZING, FlagConfig
from shared.usage_plans_schema import AgentLimits, GovernanceConfig
from tokenomics.governance_outcome import GovernanceOutcome
from tokenomics.hooks.tool_cap import ToolCapHook
from tokenomics.routing import (
    HeuristicEscalateStrategy,
    JudgeRouteDownStrategy,
    RoutingUsageTracker,
)

logger = logging.getLogger(__name__)


@dataclass
class BuiltSpecialists:
    """The specialist agents for one invocation, plus model ids (for pricing).

    The orchestrator is built by its runner (it needs the per-turn specialist
    delegation tools), so the factory produces only the specialists + the shared
    model ids here.
    """

    knowledge: Agent
    order: Agent
    orchestrator_model_id: str
    knowledge_model_id: str
    order_model_id: str
    # Per-specialist routing trackers (None when model_routing is OFF). The
    # delegation tools price each specialist's usage at tracker.last_model.
    knowledge_tracker: RoutingUsageTracker | None = None
    order_tracker: RoutingUsageTracker | None = None


def build_model(
    model_id: str,
    flags: FlagConfig,
    limits: AgentLimits | None = None,
    *,
    guardrail: bool = False,
) -> BedrockModel:
    """Construct a BedrockModel with prompt caching + governance output cap applied.

    Flag ON  -> CacheConfig(strategy="auto"): SDK places cachePoints after the
                stable prefix; Bedrock reuses the cached prefix on repeat calls.
    Flag OFF -> no cache_config: baseline, uncached.

    Governance: when the agent's limits set `max_output_tokens_per_model_call`
    (>0), it is applied as the per-call output cap (Bedrock
    `inferenceConfig.maxTokens`). 0 => no cap. Construction-time, like prompt
    caching -> applies next session.

    `guardrail=True` (the orchestrator, which receives the customer's raw text)
    applies the support Bedrock Guardrail (threat model T5). Only the latest
    user message is screened, so the guardrail cost doesn't grow with history and
    the cached prefix is untouched; a blocked input is redacted from history.
    """
    kwargs: dict = {"model_id": model_id, "region_name": config.AWS_REGION}
    if guardrail and config.SUPPORT_GUARDRAIL_ID and config.SUPPORT_GUARDRAIL_VERSION:
        kwargs.update(
            guardrail_id=config.SUPPORT_GUARDRAIL_ID,
            guardrail_version=config.SUPPORT_GUARDRAIL_VERSION,
            guardrail_latest_message=True,
            guardrail_trace="disabled",
        )
    if flags.prompt_cache.enabled:
        params = flags.prompt_cache_params()
        kwargs["cache_config"] = CacheConfig(strategy="auto", ttl=params.ttl)
    if limits and limits.output_tokens_cap():
        kwargs["max_tokens"] = limits.output_tokens_cap()
    return BedrockModel(**kwargs)


# ---- model routing (the `model_routing` pattern) -----------------------------

# The judge emits one word (SIMPLE / COMPLEX), so a tiny output cap keeps the
# per-step classify call cheap (WA AGENTCOST02-BP01: pre-classifier cost well
# below the Sonnet->Haiku tier differential it arbitrates).
_JUDGE_MAX_OUTPUT_TOKENS = 16
_JUDGE_SYSTEM_PROMPT = (
    "You classify one agent step as SIMPLE or COMPLEX for model routing. "
    "Reply with exactly one word: SIMPLE or COMPLEX."
)


@dataclass
class BuiltRouter:
    """A role's model router for one turn (model_routing pattern).

    `router` is None when the pattern is OFF (-> the caller builds the role's
    static model, the honest baseline). `judge`/`judge_model_id` are set only for
    the orchestrator (route-down LLM judge); Strands discards the judge's result,
    so the runner reads its usage off `judge.event_loop_metrics` after the turn
    and meters it as the `router_judge` agent -- exactly how the context-mgmt
    summarizer is metered.
    """

    router: ModelRouter | None = None
    judge: Agent | None = None
    judge_model_id: str = ""
    judge_strategy: JudgeRouteDownStrategy | None = None


def _build_judge_agent(judge_model_id: str, quota_hook: HookProvider | None = None) -> Agent:
    """The orchestrator's route-down LLM-as-judge (Amazon Nova Lite).

    Built WITHOUT prompt caching (its input differs every call, so a cache write
    is never read back -- same reasoning as the summarizer) and with a tiny
    output cap. It carries the shared per-user `QuotaHook` (ADR-001: every model
    call -- including a routing-decision call -- is gated by the money quota).
    """
    hooks = [quota_hook] if quota_hook is not None else []
    judge_model = BedrockModel(
        model_id=judge_model_id,
        region_name=config.AWS_REGION,
        max_tokens=_JUDGE_MAX_OUTPUT_TOKENS,
    )
    return Agent(
        model=judge_model,
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        callback_handler=None,
        hooks=hooks,
    )


def build_router(
    role: str,
    flags: FlagConfig,
    limits: AgentLimits | None = None,
    *,
    quota_hook: HookProvider | None = None,
) -> BuiltRouter:
    """A per-role `ModelRouter` when `model_routing` is ON; empty (router=None) when OFF.

    Candidate 0 is the role's CURRENT static model, so OFF and the ON default run
    the SAME model -- routing only ever moves *off* the baseline, never silently
    upgrades it (keeps the A/B honest). The fail/default direction is therefore
    set per role by which candidate is first:

      - orchestrator: [capable (Sonnet), cheap (Haiku)] + LLM judge -> route DOWN
        on "simple"; decline/error stays Sonnet (fail safe).
      - specialists : [cheap (Haiku), capable (Sonnet)] + heuristic -> escalate UP
        on clear complexity; decline/error stays Haiku (fail cheap).

    Candidates are built through `build_model`, so prompt caching and the role's
    own per-call output cap apply per candidate.
    """
    if not flags.model_routing.enabled:
        return BuiltRouter()

    p = flags.model_routing_params()
    capable_id = p.capable_model or config.ORCHESTRATOR_MODEL
    cheap_id = p.cheap_model or config.KNOWLEDGE_MODEL
    # The orchestrator's candidates carry the customer-facing guardrail too, so
    # routing never moves a turn onto an unguarded model.
    guard = role == "orchestrator"
    capable = RoutingCandidate(build_model(capable_id, flags, limits, guardrail=guard), name="capable")
    cheap = RoutingCandidate(build_model(cheap_id, flags, limits, guardrail=guard), name="cheap")

    if role == "orchestrator":
        judge_id = p.judge_model or config.ROUTER_JUDGE_MODEL
        judge = _build_judge_agent(judge_id, quota_hook)
        strategy = JudgeRouteDownStrategy("cheap", judge)
        router = ModelRouter([capable, cheap], strategy=strategy)
        return BuiltRouter(router=router, judge=judge, judge_model_id=judge_id, judge_strategy=strategy)

    # Specialists: Haiku default, heuristic escalates up to Sonnet (no judge call).
    router = ModelRouter([cheap, capable], strategy=HeuristicEscalateStrategy("capable"))
    return BuiltRouter(router=router)


class _ReconcilingRestore:
    """Mixin: restore a persisted state written by a DIFFERENT manager class.

    Strands' `restore_from_session` raises "Invalid conversation manager state"
    when the class name persisted by AgentCore Memory differs from the manager
    attached on the restoring turn. That happens whenever the pattern changes
    mid-session (flag flipped, or `auto` switching mode because prompt_cache was
    toggled, or a session started before the flag was on). The shared field
    `removed_message_count` (the restore offset into stored history) is kept, so
    the conversation continues; a prior summary is kept only by a summarizing
    manager.
    """

    def restore_from_session(self, state):  # type: ignore[no-untyped-def]
        name = type(self).__name__
        if state.get("__name__") != name:
            logger.info(
                "[context] reconciling conversation-manager state %s -> %s", state.get("__name__"), name
            )
            state = {
                **state,
                "__name__": name,
                "removed_message_count": int(state.get("removed_message_count", 0)),
            }
        return super().restore_from_session(state)  # type: ignore[misc]


class SessionSlidingWindowConversationManager(_ReconcilingRestore, SlidingWindowConversationManager):
    """Strands' sliding window, safe to restore across a mid-session pattern change."""


class SessionSummarizingConversationManager(_ReconcilingRestore, SummarizingConversationManager):
    """Strands' summarizing manager, safe to restore across a mid-session pattern change."""


@dataclass
class BuiltContextManager:
    """The orchestrator's conversation manager for one turn (context_management pattern).

    A manager is ALWAYS returned so every turn of a session attaches a class that
    can restore the session's persisted state. Pattern OFF => a sliding window with
    Strands' own defaults (window 40, trim at end of turn) -- the same behavior as
    passing nothing, the honest baseline.
    `summarizer` is set only in summarizing mode: Strands discards its result, so
    the runner reads its usage off `summarizer.event_loop_metrics` after the turn.
    """

    manager: ConversationManager
    enabled: bool = False
    strategy: str = ""
    mask_tool_history: bool = False
    summarizer: Agent | None = None
    summarizer_model_id: str = ""


def build_conversation_manager(
    flags: FlagConfig,
    orchestrator_model: BedrockModel,
    hooks: list[HookProvider] | None = None,
) -> BuiltContextManager:
    """Configure Strands' native conversation manager from the flag (no hand-rolled trimming).

    Masking (primary, both modes): prior turns' tool traffic is masked on restore
    (see `mask_restored_tool_history`), so old turns cost a few short lines each.
    Prompt cache OFF -> sliding window, trimmed before every model call (every
                        removed token saves full price; no cache to break).
    Prompt cache ON  -> summarize rarely, once the whole prompt crosses
                        `compress_at_tokens` (one cache rewrite, then cheap reads).
    `strategy` "auto" picks between them from the prompt_cache flag.

    The summarizer carries the shared per-user quota hook (ADR-001: every agent
    carries it) and is built WITHOUT prompt caching: its input differs on every
    call, so a cache write (~125%) would never be read back.
    """
    if not flags.context_management.enabled:
        return BuiltContextManager(SessionSlidingWindowConversationManager())
    p = flags.context_management_params()
    strategy = p.resolved_strategy(flags.prompt_cache.enabled)

    if strategy == CTX_SUMMARIZING:
        summarizer_model_id = p.summarizer_model or config.KNOWLEDGE_MODEL
        summarizer = Agent(
            model=build_model(summarizer_model_id, FlagConfig.all_off()),
            callback_handler=None,
            hooks=list(hooks or []),
        )
        # Strands' threshold is a ratio of the model's context window (auto-resolved
        # from the model id, e.g. 1M for Sonnet 5.5). The fallback only applies if
        # the SDK can't resolve a window for the id.
        window = orchestrator_model.context_window_limit or 1_000_000
        manager = SessionSummarizingConversationManager(
            summary_ratio=p.summary_ratio,
            preserve_recent_messages=p.preserve_recent,
            summarization_agent=summarizer,
            proactive_compression={"compression_threshold": min(1.0, p.compress_at_tokens / window)},
        )
        return BuiltContextManager(
            manager, True, strategy, p.mask_tool_history, summarizer, summarizer_model_id
        )

    # per_turn=True trims before every model call, so long tool loops within one
    # turn also stay inside the window.
    manager = SessionSlidingWindowConversationManager(window_size=p.window_size, per_turn=True)
    return BuiltContextManager(manager, True, strategy, p.mask_tool_history)


_MASKED_RESULT = "[earlier specialist answer omitted -- the reply that followed summarizes it]"


def mask_restored_tool_history(messages: list) -> int:
    """Observation masking: replace restored tool calls/results with short text, in place.

    Applied to the history AgentCore Memory restored, before the turn runs (the
    stored history is untouched). Each toolUse block becomes "[earlier call: ...]"
    and each toolResult block a fixed placeholder, so:
      * the message COUNT is unchanged -> the restore offset Strands persists
        (`removed_message_count`) stays aligned with stored history;
      * user/assistant alternation is unchanged -> valid for Bedrock Converse;
      * output is deterministic -> the masked prefix is byte-stable across turns
        (cache-friendly).
    AgentCore's native `filter_restored_tool_context` is not used: it drops
    tool-only messages, which breaks both of the first two properties.
    Returns the number of blocks masked.
    """
    masked = 0
    for msg in messages:
        content = msg.get("content") or []
        for i, block in enumerate(content):
            if "toolUse" in block:
                use = block["toolUse"]
                arg = " ".join(str(v) for v in (use.get("input") or {}).values())[:200]
                content[i] = {"text": f"[earlier call: {use.get('name', 'tool')}({arg})]"}
                masked += 1
            elif "toolResult" in block:
                content[i] = {"text": _MASKED_RESULT}
                masked += 1
    return masked


def build_specialists(
    flags: FlagConfig,
    config_: GovernanceConfig | None = None,
    hooks: list[HookProvider] | None = None,
    outcome: GovernanceOutcome | None = None,
) -> BuiltSpecialists:
    """Build the knowledge + order specialists, each with its OWN config model.

    Per-agent governance: each specialist's model is built with that agent's
    `max_output_tokens_per_model_call`. The iteration/token `Limits` for a
    specialist are applied at *call* time inside the delegation tools
    (delegation.py), since the specialist is invoked there.

    Governance integrity (ADR-001): the per-user quota/throttle `QuotaHook` is a
    shared instance passed in via `hooks` and attached to BOTH specialists, so no
    model call -- even one made directly on a specialist -- can escape the money
    quota. The factory is the single place agents are built, so attaching here
    guarantees every agent carries the hook.

    Tool-call cap (threat model T6): each specialist also gets its own
    `ToolCapHook` from its `max_tool_calls`, so a delegated call can't loop on
    tools past the plan. Needs `outcome` to record a trip; skipped without one.
    """
    config_ = config_ or GovernanceConfig.empty()
    hooks = list(hooks or [])

    def tool_cap(name: str, limits: AgentLimits) -> list[HookProvider]:
        cap = limits.tool_calls_limit()
        return [ToolCapHook(cap, outcome, agent_name=name)] if cap and outcome is not None else []

    # Model routing (specialists): heuristic escalate-up router when ON, else the
    # static Haiku model (baseline). The specialist agents still carry the shared
    # quota hook directly; the heuristic strategy makes no model call of its own.
    know_limits = config_.agent_limits("knowledge")
    order_limits = config_.agent_limits("order")
    know_router = build_router("knowledge", flags, know_limits).router
    order_router = build_router("order", flags, order_limits).router

    # Cost attribution: when routing is ON, each specialist carries its own usage
    # tracker (records which model actually ran) so the delegation tools price it
    # at the real model, not the static default. None when OFF (baseline path).
    routing_on = flags.model_routing.enabled
    know_tracker = RoutingUsageTracker() if routing_on else None
    order_tracker = RoutingUsageTracker() if routing_on else None

    knowledge = Agent(
        model=know_router or build_model(config.KNOWLEDGE_MODEL, flags, know_limits),
        system_prompt=KNOWLEDGE_PROMPT,
        tools=[search_kb],
        callback_handler=None,
        hooks=hooks + tool_cap("knowledge", know_limits) + ([know_tracker] if know_tracker else []),
    )
    order = Agent(
        model=order_router or build_model(config.ORDER_MODEL, flags, order_limits),
        system_prompt=ORDER_PROMPT,
        tools=[lookup_customer, get_orders],
        callback_handler=None,
        hooks=hooks + tool_cap("order", order_limits) + ([order_tracker] if order_tracker else []),
    )

    return BuiltSpecialists(
        knowledge=knowledge,
        order=order,
        orchestrator_model_id=config.ORCHESTRATOR_MODEL,
        knowledge_model_id=config.KNOWLEDGE_MODEL,
        order_model_id=config.ORDER_MODEL,
        knowledge_tracker=know_tracker,
        order_tracker=order_tracker,
    )
