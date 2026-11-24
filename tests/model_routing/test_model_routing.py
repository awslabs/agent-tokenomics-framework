"""Unit tests for the model-routing lever (offline -- no model calls, no AWS).

Mirrors tests/context_management/: one test per meaningful behavior. Covers the
flag schema, both routing strategies, the factory routers, cost attribution
(tracker + mixed-model pricing + the record/price functions), per-turn telemetry
+ judge metering, the benchmark roll-up, and a stubbed handle_turn on both exit
paths. No network, no AWS.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("strands")

from delegation import _priced_model
from strands.models.routing import RoutingCandidate, RoutingContext
from strands.types.exceptions import MaxTokensReachedException

import agents.orchestrator.agent as agent_mod
import config
from agents.orchestrator.pattern_telemetry import record_model_routing
from shared.flags_schema import ALL_FEATURES, MODEL_ROUTING, FlagConfig
from shared.usage_plans_schema import AgentLimits
from tokenomics import flags as flags_mod
from tokenomics.cost import UsageCollector
from tokenomics.factory import BuiltRouter, build_router
from tokenomics.identity import RunContext
from tokenomics.routing import (
    HeuristicEscalateStrategy,
    JudgeRouteDownStrategy,
    RoutingUsageTracker,
    _latest_user_text,
    selected_model_id,
)

_SONNET = "global.anthropic.claude-sonnet-5-5"
_HAIKU = "us.anthropic.claude-haiku-5-5"
_NOVA = "us.amazon.nova-lite-v1:0"


def _flags(routing: bool, **params) -> FlagConfig:
    return FlagConfig.from_dict({"model_routing": {"enabled": routing, "params": params}})


# ---- flag schema -----------------------------------------------------------


def test_schema_round_trip_and_signature():
    cfg = _flags(True, capable_model="cap", cheap_model="cheap", judge_model="judge")
    again = FlagConfig.from_dict(cfg.to_dict())
    assert again.model_routing.enabled is True
    p = again.model_routing_params()
    assert (p.capable_model, p.cheap_model, p.judge_model) == ("cap", "cheap", "judge")
    assert "model_routing=on" in again.flag_signature()
    assert MODEL_ROUTING in ALL_FEATURES


def test_missing_flag_is_off():
    cfg = FlagConfig.from_dict({"prompt_cache": {"enabled": True}})
    assert cfg.model_routing.enabled is False
    assert "model_routing" not in cfg.flag_signature()


def test_params_default_to_blank():
    p = _flags(True).model_routing_params()
    assert (p.capable_model, p.cheap_model, p.judge_model) == ("", "", "")


# ---- routing strategies (judge call stubbed) -------------------------------


def _ctx(user_text: str) -> RoutingContext:
    capable = RoutingCandidate(model=object(), name="capable")
    cheap = RoutingCandidate(model=object(), name="cheap")
    return RoutingContext(
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        system_prompt=None,
        tool_specs=[],
        candidates=(capable, cheap),
        invocation_state={},
        attempts=[],
    )


def _pick(strategy, ctx):
    return asyncio.run(strategy.select(ctx))


class _FakeJudge:
    def __init__(self, label=None, *, raises=False):
        self._label = label
        self._raises = raises

    def __call__(self, _prompt):
        if self._raises:
            raise RuntimeError("judge unavailable")
        return self._label


def test_heuristic_escalates_on_complexity():
    s = HeuristicEscalateStrategy("capable")
    assert _pick(s, _ctx("Why is my refund taking longer than the policy says?")).name == "capable"


def test_heuristic_declines_on_simple():
    s = HeuristicEscalateStrategy("capable")
    assert _pick(s, _ctx("Where is my order?")) is None  # -> Haiku default


def test_heuristic_does_not_escalate_routine_task_with_ids_and_numbers():
    # A specialist classifies the orchestrator's composed task string, which
    # routinely carries an order id, a date, and a dollar amount for a trivial
    # lookup -- none of which should escalate a routine lookup to the capable model.
    s = HeuristicEscalateStrategy("capable")
    task = "Look up the status of ORD-1001 placed 2026-10-02 for $129.99"
    assert _pick(s, _ctx(task)) is None


def test_heuristic_escalates_multi_order_compare():
    # Genuine cross-referencing carries a reasoning keyword -> escalates.
    s = HeuristicEscalateStrategy("capable")
    assert _pick(s, _ctx("Compare ORD-1001 and ORD-1002 and say which ships first")).name == "capable"


def test_judge_routes_down_on_simple():
    s = JudgeRouteDownStrategy("cheap", _FakeJudge("SIMPLE"))
    assert _pick(s, _ctx("Say hello back.")).name == "cheap"


def test_judge_declines_on_complex():
    s = JudgeRouteDownStrategy("cheap", _FakeJudge("COMPLEX"))
    assert _pick(s, _ctx("Plan the refund then reconcile both orders.")) is None


def test_judge_fails_safe_on_error():
    s = JudgeRouteDownStrategy("cheap", _FakeJudge(raises=True))
    assert _pick(s, _ctx("anything")) is None  # error -> decline -> Sonnet


# ---- _latest_user_text (what the classifier reads) -------------------------


def _ctx_messages(messages):
    return RoutingContext(
        messages=messages, system_prompt=None, tool_specs=[], candidates=(), invocation_state={}, attempts=[]
    )


def test_latest_user_text_picks_latest_turn():
    ctx = _ctx_messages(
        [
            {"role": "user", "content": [{"text": "first question"}]},
            {"role": "assistant", "content": [{"text": "answer"}]},
            {"role": "user", "content": [{"text": "second question"}]},
        ]
    )
    assert _latest_user_text(ctx) == "second question"


def test_latest_user_text_tool_result_only_yields_empty():
    # A user message carrying only a toolResult (no text) -> "" (common on the
    # specialist path); the heuristic then treats it as not-complex.
    ctx = _ctx_messages([{"role": "user", "content": [{"toolResult": {"x": 1}}]}])
    assert _latest_user_text(ctx) == ""


# ---- factory: build_router -------------------------------------------------


def _routing_flags(on: bool) -> FlagConfig:
    return FlagConfig.from_dict({"model_routing": {"enabled": on}})


def test_build_router_off_returns_none():
    br = build_router("orchestrator", _routing_flags(False))
    assert isinstance(br, BuiltRouter) and br.router is None and br.judge is None


def test_orchestrator_router_defaults_to_capable_and_has_judge():
    br = build_router("orchestrator", _routing_flags(True))
    assert br.router.candidates[0].name == "capable"  # fail-safe default = Sonnet
    assert br.judge is not None and br.judge_model_id == config.ROUTER_JUDGE_MODEL


def test_specialist_router_defaults_to_cheap_no_judge():
    br = build_router("knowledge", _routing_flags(True))
    assert br.router.candidates[0].name == "cheap"  # fail-cheap default = Haiku
    assert br.judge is None


def test_build_router_param_overrides_models():
    flags = FlagConfig.from_dict(
        {
            "model_routing": {
                "enabled": True,
                "params": {"capable_model": "cap-x", "cheap_model": "cheap-y", "judge_model": "judge-z"},
            }
        }
    )
    br = build_router("orchestrator", flags)
    ids = {c.name: c.model.get_config()["model_id"] for c in br.router.candidates}
    assert ids == {"capable": "cap-x", "cheap": "cheap-y"}
    assert br.judge_model_id == "judge-z"


def test_build_router_applies_output_cap_per_candidate():
    limits = AgentLimits(max_output_tokens_per_model_call=256)
    br = build_router("knowledge", _routing_flags(True), limits)
    for c in br.router.candidates:
        assert c.model.get_config().get("max_tokens") == 256


# ---- cost attribution: selected model + tracker + pricing ------------------


class _FakeModel:
    def __init__(self, mid):
        self._mid = mid

    def get_config(self):
        return {"model_id": self._mid}


def _routing_state(mid):
    return {"strands:model_routing:abc:def": SimpleNamespace(model=_FakeModel(mid))}


def test_selected_model_id_reads_routing_state():
    assert selected_model_id(_routing_state(_HAIKU)) == _HAIKU
    assert selected_model_id({}) is None


def test_selected_model_id_prefix_matches_real_sdk_constant():
    # Guard against the SDK renaming its routing-state key: our extraction prefix
    # must match the real constant, or attribution silently breaks.
    from strands.models.routing import router as _r

    from tokenomics.routing import _ROUTING_STATE_PREFIX

    sdk_prefix = getattr(_r, "_ROUTING_KEY_PREFIX", None)
    assert sdk_prefix is not None
    assert sdk_prefix.startswith(_ROUTING_STATE_PREFIX) or _ROUTING_STATE_PREFIX.startswith(sdk_prefix)


def test_tracker_mixed_models_last_wins_and_tallies_both():
    tr = RoutingUsageTracker()
    tr._after(SimpleNamespace(invocation_state=_routing_state(_HAIKU)))
    tr._after(SimpleNamespace(invocation_state=_routing_state(_SONNET)))
    assert tr.last_model == _SONNET
    assert tr.model_calls == {_HAIKU: 1, _SONNET: 1}


def test_mixed_model_pricing_sums_per_model():
    # A routed turn can span Sonnet + Haiku + a Nova judge; each bucket is priced
    # at its OWN model, and the turn total equals the sum of the per-agent costs.
    c = UsageCollector()
    c.record_usage(_SONNET, {"inputTokens": 100, "outputTokens": 50}, agent="orchestrator")
    c.record_usage(_HAIKU, {"inputTokens": 200, "outputTokens": 20}, agent="knowledge")
    c.record_usage(_NOVA, {"inputTokens": 30, "outputTokens": 2}, agent="router_judge")
    d = c.to_dict()
    assert d["by_agent"]["orchestrator"]["model_id"] == _SONNET
    assert d["by_agent"]["knowledge"]["model_id"] == _HAIKU
    assert d["by_agent"]["router_judge"]["model_id"] == _NOVA
    assert abs(d["cost_usd"] - sum(d["by_agent"][a]["cost_usd"] for a in d["by_agent"])) < 1e-6


def test_priced_model_prefers_tracked_then_default():
    assert _priced_model(None, _HAIKU) == _HAIKU
    tr = RoutingUsageTracker()
    assert _priced_model(tr, _HAIKU) == _HAIKU  # last_model "" -> default
    tr.last_model = _SONNET
    assert _priced_model(tr, _HAIKU) == _SONNET  # routed -> tracked id


# ---- per-turn telemetry + judge metering (_record_model_routing) -----------


def _tracker(model_calls: dict) -> RoutingUsageTracker:
    tr = RoutingUsageTracker()
    tr.model_calls = dict(model_calls)
    if model_calls:
        tr.last_model = list(model_calls)[-1]
    return tr


def test_record_model_routing_off_returns_none():
    built = SimpleNamespace(knowledge_tracker=None, order_tracker=None)
    assert record_model_routing(_routing_flags(False), None, None, built, UsageCollector()) is None


def test_record_model_routing_meters_judge_and_builds_block():
    orch_tracker = _tracker({config.KNOWLEDGE_MODEL: 1})  # routed DOWN to cheap
    know_tracker = _tracker({config.ORCHESTRATOR_MODEL: 1})  # escalated UP to capable
    built = SimpleNamespace(knowledge_tracker=know_tracker, order_tracker=None)
    # Judge spend comes from the strategy's per-call tally, not an end-of-turn read.
    orch_router = SimpleNamespace(
        judge=None,
        judge_model_id=config.ROUTER_JUDGE_MODEL,
        judge_strategy=SimpleNamespace(
            calls=2,
            usage={
                "inputTokens": 40,
                "outputTokens": 3,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
            },
        ),
    )
    collector = UsageCollector()
    block = record_model_routing(_routing_flags(True), orch_router, orch_tracker, built, collector)

    assert block["enabled"] is True
    assert block["judge_calls"] == 2 and block["judge_cost_usd"] > 0
    assert collector.by_agent["router_judge"].model_id == config.ROUTER_JUDGE_MODEL
    assert block["orchestrator_cheap_calls"] == 1
    assert block["specialist_escalated_calls"] == 1
    assert block["cheap_calls"] == 1 and block["capable_calls"] == 1
    assert block["model_calls"] == 2


class _CountingJudge:
    """Judge stub that accumulates usage per call, like a reused Strands Agent."""

    def __init__(self, label="SIMPLE", per_call=(10, 2)):
        self._label = label
        self._in, self._out = per_call
        self.messages = []
        self.event_loop_metrics = SimpleNamespace(accumulated_usage={"inputTokens": 0, "outputTokens": 0})

    def __call__(self, _prompt):
        self.event_loop_metrics.accumulated_usage["inputTokens"] += self._in
        self.event_loop_metrics.accumulated_usage["outputTokens"] += self._out
        return self._label


def test_judge_usage_tally_accumulates_across_calls():
    # A judge called N times in a turn must contribute N calls' worth of tokens
    # to router_judge, not just the last call's.
    j = _CountingJudge(per_call=(10, 2))
    s = JudgeRouteDownStrategy("cheap", j)
    for _ in range(3):
        _pick(s, _ctx("where is my order"))
    assert s.calls == 3
    assert s.usage["inputTokens"] == 30  # 3 x 10, per-call deltas summed
    assert s.usage["outputTokens"] == 6  # 3 x 2


def test_record_side_agent_meters_and_returns_cost():
    c = UsageCollector()
    side = SimpleNamespace(
        event_loop_metrics=SimpleNamespace(accumulated_usage={"inputTokens": 100, "outputTokens": 10})
    )
    cost = c.record_side_agent(side, _HAIKU, "summarizer")
    assert cost > 0
    assert c.by_agent["summarizer"].model_id == _HAIKU
    assert c.by_agent["summarizer"].cost_usd == cost
    # Nothing to record -> 0.0, no bucket created.
    empty = SimpleNamespace(event_loop_metrics=SimpleNamespace(accumulated_usage={}))
    assert c.record_side_agent(empty, _HAIKU, "x") == 0.0
    assert "x" not in c.by_agent


# ---- benchmark roll-up: control-plane metrics + _feature_stats -------------


def _load_cp_metrics():
    import importlib.util
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "src", "control-plane", "lib", "metrics.py")
    spec = importlib.util.spec_from_file_location("cp_metrics_routing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_metrics_aggregate_rolls_up_routing():
    metrics = _load_cp_metrics()
    convs = [
        {
            "usage": {
                "cost_usd": 0.01,
                "total_input_tokens": 100,
                "model_routing": {
                    "model_calls": 2,
                    "capable_calls": 1,
                    "cheap_calls": 1,
                    "orchestrator_cheap_calls": 1,
                    "specialist_escalated_calls": 0,
                    "judge_cost_usd": 0.00001,
                },
            }
        },
        {
            "usage": {
                "cost_usd": 0.02,
                "total_input_tokens": 200,
                "model_routing": {
                    "model_calls": 1,
                    "capable_calls": 1,
                    "cheap_calls": 0,
                    "orchestrator_cheap_calls": 0,
                    "specialist_escalated_calls": 1,
                    "judge_cost_usd": 0.00002,
                },
            }
        },
    ]
    agg = metrics.aggregate(convs)
    assert agg["routing_capable_calls"] == 2
    assert agg["routing_cheap_calls"] == 1
    assert agg["routing_orch_cheap_calls"] == 1
    assert agg["routing_escalated_calls"] == 1
    assert agg["routing_model_calls"] == 3
    assert abs(agg["routing_judge_cost_usd"] - 0.00003) < 1e-9
    # Absent block -> keys stay zero (independently deployable planes).
    assert metrics.aggregate([{"usage": {"cost_usd": 0.0}}])["routing_model_calls"] == 0


def test_feature_stats_routing_branch():
    import importlib
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if (p := os.path.join(root, "src", "control-plane")) not in sys.path:
        sys.path.insert(0, p)
    for var in ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE"):
        os.environ.setdefault(var, f"test-{var.lower()}")
    try:
        bm = importlib.import_module("handlers.benchmarks")
    except Exception as exc:
        pytest.skip(f"control-plane benchmarks not importable offline: {exc!r}")

    m = {
        "routing_capable_calls": 3,
        "routing_cheap_calls": 7,
        "routing_orch_cheap_calls": 5,
        "routing_escalated_calls": 1,
        "routing_judge_cost_usd": 0.0012,
    }
    stats = {s["key"]: s["value"] for s in bm._feature_stats(["model_routing"], m)}
    assert stats["Routing distribution"] == "Capable 30% · Cheap 70%"
    assert stats["Orchestrator steps routed to cheap model"] == "5"
    assert stats["Judge cost"] == "$0.0012"
    # divide-by-zero guard: no distribution row when there were no routed calls.
    assert "Routing distribution" not in {s["key"] for s in bm._feature_stats(["model_routing"], {})}


# ---- handle_turn integration (stubbed orchestrator -- no Bedrock) ----------


def _fake_agent_class(*, result=None, raises=None, routed_model=None):
    class _FakeAgent:
        def __init__(self, **kwargs):
            self.messages = []
            self.event_loop_metrics = SimpleNamespace(
                accumulated_usage={"inputTokens": 60, "outputTokens": 8}
            )
            self._hooks = kwargs.get("hooks") or []

        def __call__(self, message=None, **kw):
            # Simulate the router having run on `routed_model` by firing this
            # agent's RoutingUsageTracker, exactly as a real model call would.
            if routed_model is not None:
                for h in self._hooks:
                    if isinstance(h, RoutingUsageTracker):
                        h._after(
                            SimpleNamespace(
                                invocation_state={
                                    "strands:model_routing:x": SimpleNamespace(model=_FakeModel(routed_model))
                                }
                            )
                        )
            if raises is not None:
                raise raises
            return result

    return _FakeAgent


def _run_turn(monkeypatch, fake_cls):
    monkeypatch.setattr(agent_mod, "Agent", fake_cls)
    flags_mod.set_override(_routing_flags(True))
    try:
        ctx = RunContext(user_id="itest-user", profile={"name": "Bob", "tier": "prime"}, run_id=None)
        return agent_mod.handle_turn("What can you help with?", ctx)
    finally:
        flags_mod.set_override(None)


def test_handle_turn_routing_on_normal_path(monkeypatch):
    result_obj = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(accumulated_usage={"inputTokens": 100, "outputTokens": 20}),
    )
    tr = _run_turn(monkeypatch, _fake_agent_class(result=result_obj))
    assert tr.usage.get("model_routing", {}).get("enabled") is True
    assert "router_judge" in tr.model_ids
    # Single recording -> no double count; tracker never fired -> static fallback.
    assert tr.usage["by_agent"]["orchestrator"]["model_calls"] == 1
    assert tr.usage["by_agent"]["orchestrator"]["model_id"] == config.ORCHESTRATOR_MODEL


def test_handle_turn_routing_on_maxtokens_path(monkeypatch):
    tr = _run_turn(monkeypatch, _fake_agent_class(raises=MaxTokensReachedException("cap")))
    assert tr.usage.get("model_routing", {}).get("enabled") is True
    assert tr.usage["by_agent"]["orchestrator"]["model_calls"] == 1
    assert tr.reply  # non-empty fallback reply


def test_handle_turn_prices_orchestrator_at_routed_model(monkeypatch):
    # When the orchestrator routes DOWN, its usage must be priced at the model
    # that actually ran (Haiku), not the static Sonnet default -- so the
    # control-plane MODEL# roll-up (built from by_agent[...].model_id) is correct.
    result_obj = SimpleNamespace(
        stop_reason="end_turn",
        metrics=SimpleNamespace(accumulated_usage={"inputTokens": 100, "outputTokens": 20}),
    )
    tr = _run_turn(monkeypatch, _fake_agent_class(result=result_obj, routed_model=_HAIKU))
    assert tr.usage["by_agent"]["orchestrator"]["model_id"] == _HAIKU
    assert tr.usage["by_agent"]["orchestrator"]["model_id"] != config.ORCHESTRATOR_MODEL
