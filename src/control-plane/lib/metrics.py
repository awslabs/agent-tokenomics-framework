"""Benchmark metric aggregation.

Rolls up the per-conversation usage the agent returns into a run-level summary
(`aggregate`) and a per-agent cost/token breakdown (`per_agent`). The A/B delta
is computed by the benchmark handler (`handlers.benchmarks._comparison`) and
embedded in the benchmark record, so there is no separate compare pass here.
"""

from __future__ import annotations

from typing import Any

from shared.price_map import price_for


def _cache_costs(usage: dict[str, Any]) -> tuple[float, float]:
    """(cache-read $, cache-write $) for one turn, priced per agent's model.

    Shown separately so a cache break (a cache-write spike) is visible, not
    hidden inside the total.
    """
    read = write = 0.0
    for a in (usage.get("by_agent") or {}).values():
        a = a or {}
        price = price_for(a.get("model_id", ""))
        read += int(a.get("cache_read_tokens", 0)) / 1000 * price.cache_read
        write += int(a.get("cache_write_tokens", 0)) / 1000 * price.cache_write
    return read, write


def aggregate(conversations: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-conversation usage dicts into a run-level summary."""
    totals = {
        "conversations": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_input_tokens": 0,
        "model_calls": 0,
        "cost_usd": 0.0,
        "cache_read_cost_usd": 0.0,
        "cache_write_cost_usd": 0.0,
        # context_management pattern (zero when OFF)
        "messages_removed": 0,
        "tool_blocks_masked": 0,
        "summarizations": 0,
        "summarizer_cost_usd": 0.0,
        # model_routing pattern (zero when OFF)
        "routing_model_calls": 0,
        "routing_capable_calls": 0,  # total calls on the capable tier (distribution)
        "routing_cheap_calls": 0,  # total calls on the cheap tier (distribution)
        "routing_orch_cheap_calls": 0,  # orchestrator steps routed DOWN to cheap
        "routing_escalated_calls": 0,  # specialist steps escalated UP to capable
        "routing_judge_cost_usd": 0.0,
    }
    for conv in conversations:
        usage = conv.get("usage") or {}
        totals["conversations"] += 1
        totals["input_tokens"] += int(usage.get("input_tokens", 0))
        totals["output_tokens"] += int(usage.get("output_tokens", 0))
        totals["cache_read_tokens"] += int(usage.get("cache_read_tokens", 0))
        totals["cache_write_tokens"] += int(usage.get("cache_write_tokens", 0))
        totals["total_input_tokens"] += int(usage.get("total_input_tokens", 0))
        totals["model_calls"] += int(usage.get("model_calls", 0))
        totals["cost_usd"] += float(usage.get("cost_usd", 0.0))
        read, write = _cache_costs(usage)
        totals["cache_read_cost_usd"] += read
        totals["cache_write_cost_usd"] += write
        ctx = usage.get("context_mgmt") or {}
        totals["messages_removed"] += int(ctx.get("messages_removed", 0))
        totals["tool_blocks_masked"] += int(ctx.get("tool_blocks_masked", 0))
        totals["summarizations"] += 1 if ctx.get("summarized") else 0
        # Internal component (inside the orchestrator's by_agent bucket too).
        summarizer = (usage.get("by_component") or {}).get("summarizer") or {}
        totals["summarizer_cost_usd"] += float(summarizer.get("cost_usd", 0.0))

        mr = usage.get("model_routing") or {}
        totals["routing_model_calls"] += int(mr.get("model_calls", 0))
        # Tier counts are labelled producer-side from the configured ids.
        totals["routing_capable_calls"] += int(mr.get("capable_calls", 0))
        totals["routing_cheap_calls"] += int(mr.get("cheap_calls", 0))
        totals["routing_orch_cheap_calls"] += int(mr.get("orchestrator_cheap_calls", 0))
        totals["routing_escalated_calls"] += int(mr.get("specialist_escalated_calls", 0))
        totals["routing_judge_cost_usd"] += float(mr.get("judge_cost_usd", 0.0))

    convs = max(totals["conversations"], 1)
    denom = max(totals["total_input_tokens"], 1)
    totals["cache_hit_ratio"] = round(totals["cache_read_tokens"] / denom, 4)
    totals["cost_per_conversation"] = round(totals["cost_usd"] / convs, 6)
    for key in (
        "cost_usd",
        "cache_read_cost_usd",
        "cache_write_cost_usd",
        "summarizer_cost_usd",
        "routing_judge_cost_usd",
    ):
        totals[key] = round(totals[key], 6)
    return totals


def per_turn(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average input tokens + cost at each turn index across sessions.

    This is the "cost per turn climbs without context management, flattens with
    it" series. Built from the per-turn records the load generator already keeps.
    """
    buckets: dict[int, dict[str, Any]] = {}
    for conv in conversations:
        if conv.get("error"):
            continue
        usage = conv.get("usage") or {}
        b = buckets.setdefault(int(conv.get("turn", 0)), {"n": 0, "input": 0, "cost": 0.0})
        b["n"] += 1
        b["input"] += int(usage.get("total_input_tokens", 0))
        b["cost"] += float(usage.get("cost_usd", 0.0))
    return [
        {
            "turn": t + 1,
            "avg_input_tokens": round(b["input"] / b["n"]),
            "avg_cost_usd": round(b["cost"] / b["n"], 6),
        }
        for t, b in sorted(buckets.items())
    ]


def per_agent(conversations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Sum each turn record's usage["by_agent"] into per-agent buckets.

    Returns a dict keyed by agent name (orchestrator / knowledge / order) ->
    { cost_usd, input_tokens, output_tokens, cache_read_tokens, model_calls }.
    Only agents that actually appear in the usage are included. The handler
    persists this as the run doc's `per_agent` breakdown.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for conv in conversations:
        usage = conv.get("usage") or {}
        by_agent = usage.get("by_agent") or {}
        for name, agent_usage in by_agent.items():
            agent_usage = agent_usage or {}
            bucket = buckets.setdefault(
                name,
                {
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "model_calls": 0,
                },
            )
            bucket["cost_usd"] += float(agent_usage.get("cost_usd", 0.0))
            bucket["input_tokens"] += int(agent_usage.get("input_tokens", 0))
            bucket["output_tokens"] += int(agent_usage.get("output_tokens", 0))
            bucket["cache_read_tokens"] += int(agent_usage.get("cache_read_tokens", 0))
            bucket["model_calls"] += int(agent_usage.get("model_calls", 0))

    for bucket in buckets.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return buckets
