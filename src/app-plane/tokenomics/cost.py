"""Cost attribution: turn Bedrock usage token counts into dollars.

A UsageCollector accumulates per-model-call token buckets across a whole
invocation (orchestrator + specialists) and dollarizes each agent's usage with
the correct per-model price -- Sonnet, Haiku, and Nova bill differently, so we
must not sum tokens across models before pricing.

It keeps BOTH a flat total for the turn AND a per-agent breakdown (orchestrator
/ knowledge / order), so the UI can show which agent drove the cost and how many
model calls each made -- the core "cost attribution per agent" tokenomics story.

Internal components that serve an agent (e.g. the context-management
`summarizer`, which compacts the orchestrator's history) are NOT agents: their
spend is counted in the owning agent's bucket, so per-agent figures still sum to
the turn total, and is also broken out under `by_component` for detail.

Source of the numbers: every Strands `AgentResult` carries
`metrics.accumulated_usage` with the Bedrock token buckets for that agent's run
(inputTokens, outputTokens, cacheReadInputTokens, cacheWriteInputTokens). We
read those counts Bedrock already returns and build only the dollar delta on top
-- reuse, not rebuild.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from shared.price_map import cost_usd

logger = logging.getLogger(__name__)


@dataclass
class UsageTotals:
    """Aggregated token buckets + dollars (for the whole turn or one agent)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    # Model id this (agent) ran on. Empty for the flat turn total (mixed models).
    model_id: str = ""

    @property
    def total_input_tokens(self) -> int:
        """Total input incl. cache (Bedrock's inputTokens excludes cached when caching is on)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of input tokens served from cache. 0.0 when caching is off."""
        denom = self.total_input_tokens
        return (self.cache_read_tokens / denom) if denom else 0.0

    def add(self, model_id: str, it: int, ot: int, cr: int, cw: int, set_model: bool = True) -> None:
        """Add one call's buckets, priced at `model_id`.

        `set_model=False` keeps this bucket's `model_id` (used when a component on
        another model, e.g. a Haiku summarizer, is folded into an agent's bucket --
        the bucket must keep naming the agent's own model, which cache-cost
        roll-ups price against).
        """
        self.input_tokens += it
        self.output_tokens += ot
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        self.model_calls += 1
        self.cost_usd += cost_usd(
            model_id,
            input_tokens=it,
            output_tokens=ot,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
        )
        if model_id and set_model:
            self.model_id = model_id

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_input_tokens"] = self.total_input_tokens
        d["cache_hit_ratio"] = round(self.cache_hit_ratio, 4)
        d["cost_usd"] = round(self.cost_usd, 6)
        return d


class UsageCollector:
    """Accumulates usage across agent invocations within one customer turn."""

    def __init__(self) -> None:
        self.totals = UsageTotals()
        # Per-agent breakdown, keyed by agent name (orchestrator/knowledge/order).
        self.by_agent: dict[str, UsageTotals] = {}
        # Internal components (e.g. summarizer), keyed by component name. Their
        # spend is ALSO inside the owning agent's by_agent bucket + the totals.
        self.by_component: dict[str, UsageTotals] = {}

    def record_usage(
        self,
        model_id: str,
        usage: dict[str, Any],
        agent: str = "",
        component: str = "",
    ) -> float:
        """Record one usage bundle under its model's price; return its cost (USD).

        Updates the flat turn total AND (when `agent` is given) that agent's own
        breakdown bucket. With `component`, the usage belongs to an internal
        component serving `agent`: it is added to that agent's bucket (without
        changing the bucket's model) and to its own `by_component` bucket. The
        returned cost lets callers avoid a fragile by_agent[...] re-lookup to read
        what they just recorded.
        """
        it = int(usage.get("inputTokens", 0) or 0)
        ot = int(usage.get("outputTokens", 0) or 0)
        cr = int(usage.get("cacheReadInputTokens", 0) or 0)
        cw = int(usage.get("cacheWriteInputTokens", 0) or 0)

        self.totals.add(model_id, it, ot, cr, cw)
        if agent:
            bucket = self.by_agent.setdefault(agent, UsageTotals())
            bucket.add(model_id, it, ot, cr, cw, set_model=not component)
        if component:
            self.by_component.setdefault(component, UsageTotals()).add(model_id, it, ot, cr, cw)
        return cost_usd(
            model_id,
            input_tokens=it,
            output_tokens=ot,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
        )

    def record_result(self, model_id: str, agent_result: Any, agent: str = "") -> None:
        """Convenience: pull accumulated_usage off a Strands AgentResult."""
        usage = _usage_from_result(agent_result)
        if usage:
            self.record_usage(model_id, usage, agent=agent)

    def record_side_agent(
        self,
        agent_obj: Any,
        model_id: str,
        agent: str,
        component: str = "",
    ) -> float:
        """Record a side object's OWN `event_loop_metrics.accumulated_usage` under
        its model; return its cost (0.0 if nothing to record).

        Side objects (the context-mgmt summarizer, the orchestrator's own recovery
        on a max-tokens cutoff) are metered off their event-loop metrics rather
        than an AgentResult. One guarded helper instead of copy-pasted try/except
        blocks. Pass `component` to fold an internal component's spend into the
        owning `agent`'s bucket (and break it out under `by_component`) instead of
        giving it its own agent bucket.
        """
        try:
            metrics = getattr(agent_obj, "event_loop_metrics", None)
            acc = getattr(metrics, "accumulated_usage", None)
            if acc and (acc.get("inputTokens") or acc.get("outputTokens")):
                return self.record_usage(model_id, dict(acc), agent=agent, component=component)
        except Exception:  # best-effort cost recording, logged at DEBUG
            # Side-agent telemetry is best-effort and runs per turn; a miss just
            # omits a small cost delta, so log at DEBUG to avoid per-turn noise.
            logger.debug(
                "side-agent cost not recorded (agent=%s, component=%s, model=%s)",
                agent,
                component,
                model_id,
                exc_info=True,
            )
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        """Turn total plus a per-agent breakdown (and any component detail) for the UI."""
        d = self.totals.to_dict()
        d["by_agent"] = {name: b.to_dict() for name, b in self.by_agent.items()}
        if self.by_component:
            d["by_component"] = {name: b.to_dict() for name, b in self.by_component.items()}
        return d


def _usage_from_result(agent_result: Any) -> dict[str, Any] | None:
    metrics = getattr(agent_result, "metrics", None)
    if metrics is None:
        return None
    usage = getattr(metrics, "accumulated_usage", None)
    if not usage:
        return None
    return dict(usage)
