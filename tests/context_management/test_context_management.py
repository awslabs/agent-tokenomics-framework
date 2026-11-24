"""Unit tests for the context-management lever (offline -- no model calls, no AWS).

Covers: flag schema round-trip + param clamping, `auto` picking the right Strands
manager from the prompt_cache flag, the summarizer being built cheap + governed,
summarizer cost counted inside the orchestrator (+ by_component), manager state surviving a
session restore (incl. a mid-session lever change), count-preserving observation
masking, the chat API surfacing agent errors, and the control-plane per-turn /
cache-cost roll-ups.
"""

from __future__ import annotations

import importlib.util
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("strands")

from strands.agent.conversation_manager import (
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)

import config
from shared.flags_schema import FlagConfig
from tokenomics.cost import UsageCollector
from tokenomics.factory import (
    SessionSlidingWindowConversationManager,
    SessionSummarizingConversationManager,
    build_conversation_manager,
    build_model,
    mask_restored_tool_history,
)


def _flags(cache: bool, ctx: bool = True, **params) -> FlagConfig:
    return FlagConfig.from_dict(
        {
            "memory": {"enabled": True},
            "prompt_cache": {"enabled": cache},
            "context_management": {"enabled": ctx, "params": params},
        }
    )


def _orch_model(flags: FlagConfig):
    return build_model(config.ORCHESTRATOR_MODEL, flags)


# ---- schema ----------------------------------------------------------------


def test_schema_round_trip_and_signature():
    cfg = _flags(cache=True, strategy="summarizing", window_size=8)
    again = FlagConfig.from_dict(cfg.to_dict())
    assert again.context_management.enabled is True
    assert again.context_management_params().strategy == "summarizing"
    assert again.context_management_params().window_size == 8
    assert "context_management=on" in again.flag_signature()


def test_defaults_and_clamping():
    p = FlagConfig.all_off().context_management_params()
    assert (
        p.mask_tool_history,
        p.strategy,
        p.window_size,
        p.compress_at_tokens,
        p.summary_ratio,
        p.preserve_recent,
    ) == (True, "auto", 12, 20000, 0.6, 4)
    bad = _flags(
        cache=False, strategy="bogus", summary_ratio=5, window_size=-3, compress_at_tokens=10
    ).context_management_params()
    assert bad.strategy == "auto"
    assert bad.summary_ratio == 0.8
    assert bad.window_size == 2
    assert bad.compress_at_tokens == 500


def test_missing_flag_is_off():
    cfg = FlagConfig.from_dict({"prompt_cache": {"enabled": True}})
    assert cfg.context_management.enabled is False


# ---- factory ---------------------------------------------------------------


def test_flag_off_attaches_strands_default_equivalent():
    """OFF still attaches a manager (same class every turn) with Strands' defaults."""
    flags = _flags(cache=True, ctx=False)
    built = build_conversation_manager(flags, _orch_model(flags))
    assert isinstance(built.manager, SessionSlidingWindowConversationManager)
    assert built.manager.window_size == 40  # Strands default
    assert built.manager.per_turn is False  # Strands default
    assert built.enabled is False and built.summarizer is None and built.mask_tool_history is False


def test_auto_cache_off_picks_sliding_window():
    flags = _flags(cache=False)
    built = build_conversation_manager(flags, _orch_model(flags))
    assert isinstance(built.manager, SlidingWindowConversationManager)
    assert built.strategy == "sliding_window"
    assert built.manager.window_size == 12
    assert built.summarizer is None


def test_auto_cache_on_picks_summarizing_with_token_threshold():
    flags = _flags(cache=True)
    model = _orch_model(flags)
    built = build_conversation_manager(flags, model)
    assert isinstance(built.manager, SummarizingConversationManager)
    assert built.strategy == "summarizing"
    assert model.context_window_limit == 1_000_000  # auto-resolved for Sonnet 5.5
    assert built.manager._compression_threshold == pytest.approx(20000 / 1_000_000)
    assert built.manager.preserve_recent_messages == 4
    assert built.manager.summary_ratio == 0.6


def test_explicit_strategy_overrides_auto():
    flags = _flags(cache=True, strategy="sliding_window")
    built = build_conversation_manager(flags, _orch_model(flags))
    assert isinstance(built.manager, SlidingWindowConversationManager)


def test_summarizer_is_uncached_cheap_and_governed():
    class Marker:
        def register_hooks(self, registry, **kwargs):
            self.registered = True

    hook = Marker()
    flags = _flags(cache=True)
    built = build_conversation_manager(flags, _orch_model(flags), hooks=[hook])
    assert built.summarizer_model_id == config.KNOWLEDGE_MODEL
    assert built.summarizer.model.config.get("cache_config") is None
    assert getattr(hook, "registered", False), "quota hook must be attached to the summarizer"


# ---- runner: summarizer cost + trim stats ------------------------------------


def test_summarizer_cost_counted_inside_orchestrator():
    """The summarizer is a component, not an agent: no by_agent entry of its own."""
    from agents.orchestrator.pattern_telemetry import record_context_mgmt

    flags = _flags(cache=True)
    built = build_conversation_manager(flags, _orch_model(flags))
    built.summarizer.event_loop_metrics.accumulated_usage.update(
        {"inputTokens": 3000, "outputTokens": 200, "totalTokens": 3200}
    )
    built.manager.removed_message_count = 7
    collector = UsageCollector()
    # The orchestrator's own (Sonnet) usage is recorded first, as in handle_turn.
    collector.record_usage(
        config.ORCHESTRATOR_MODEL,
        {"inputTokens": 1000, "outputTokens": 100, "cacheReadInputTokens": 5000},
        agent="orchestrator",
    )
    fake_orch = SimpleNamespace(messages=[{}] * 6)

    stats = record_context_mgmt(built, fake_orch, collector, messages_restored=13, removed_before=0)

    usage = collector.to_dict()
    assert set(usage["by_agent"]) == {"orchestrator"}  # no "summarizer" agent
    comp = usage["by_component"]["summarizer"]
    assert comp["input_tokens"] == 3000 and comp["cost_usd"] > 0
    assert comp["model_id"] == config.KNOWLEDGE_MODEL
    orch = usage["by_agent"]["orchestrator"]
    assert orch["input_tokens"] == 4000  # 1000 own + 3000 summarizer
    assert orch["model_id"] == config.ORCHESTRATOR_MODEL  # not overwritten by Haiku
    # Money reconciles: per-agent sum == turn total.
    assert sum(a["cost_usd"] for a in usage["by_agent"].values()) == pytest.approx(usage["cost_usd"])
    assert stats == {
        "strategy": "summarizing",
        "messages_restored": 13,
        "tool_blocks_masked": 0,
        "messages_after": 6,
        "messages_removed": 7,
        "summarized": True,
    }


def test_no_stats_when_flag_off():
    from agents.orchestrator.pattern_telemetry import record_context_mgmt

    flags = _flags(cache=False, ctx=False)
    built = build_conversation_manager(flags, _orch_model(flags))
    assert record_context_mgmt(built, SimpleNamespace(messages=[]), UsageCollector(), 0, 0) is None


def test_summary_state_survives_restore():
    """AgentCore Memory persists get_state(); the next turn's manager restores it."""
    flags = _flags(cache=True)
    first = build_conversation_manager(flags, _orch_model(flags)).manager
    summary = {"role": "user", "content": [{"text": "Summary: customer asked about returns."}]}
    first._summary_message = summary
    first.removed_message_count = 5

    nxt = build_conversation_manager(flags, _orch_model(flags)).manager
    prepend = nxt.restore_from_session(first.get_state())
    assert prepend == [summary]
    assert nxt.removed_message_count == 5


# ---- control plane roll-ups ---------------------------------------------------


def _load_metrics():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "control-plane", "lib", "metrics.py")
    spec = importlib.util.spec_from_file_location("cp_metrics", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _turn(turn: int, total_in: int, cost: float, **extra):
    return {"turn": turn, "usage": {"total_input_tokens": total_in, "cost_usd": cost, **extra}}


def test_per_turn_averages_across_sessions():
    m = _load_metrics()
    convs = [_turn(0, 1000, 0.01), _turn(1, 3000, 0.03), _turn(0, 2000, 0.02), _turn(1, 5000, 0.05)]
    assert m.per_turn(convs) == [
        {"turn": 1, "avg_input_tokens": 1500, "avg_cost_usd": 0.015},
        {"turn": 2, "avg_input_tokens": 4000, "avg_cost_usd": 0.04},
    ]


def test_aggregate_splits_cache_cost_and_ctx_stats():
    m = _load_metrics()
    by_agent = {
        "orchestrator": {
            "model_id": "global.anthropic.claude-sonnet-5-5",
            "cache_read_tokens": 10_000,
            "cache_write_tokens": 1_000,
        },
    }
    by_component = {"summarizer": {"model_id": "us.anthropic.claude-haiku-5-5", "cost_usd": 0.004}}
    convs = [
        _turn(
            0,
            0,
            0.1,
            by_agent=by_agent,
            by_component=by_component,
            context_mgmt={"messages_removed": 6, "summarized": True},
        )
    ]
    out = m.aggregate(convs)
    # Sonnet 5.5 cache rates: read 0.0001, write 0.0025 per 1K (x1000 tokens below).
    assert out["cache_read_cost_usd"] == pytest.approx(10 * 0.0001)
    assert out["cache_write_cost_usd"] == pytest.approx(1 * 0.0025)
    assert out["messages_removed"] == 6
    assert out["summarizations"] == 1
    assert out["summarizer_cost_usd"] == pytest.approx(0.004)


# ---- restore across turns (PR bug: "Invalid conversation manager state") ------


@pytest.mark.parametrize(
    "first,second",
    [
        (dict(cache=True), dict(cache=True)),  # all ON, same mode every turn
        (dict(cache=True), dict(cache=False)),  # cache toggled -> auto flips mode
        (dict(cache=False), dict(cache=True)),
        (dict(cache=True, ctx=False), dict(cache=True)),  # session started with lever OFF
        (dict(cache=True), dict(cache=True, ctx=False)),  # lever turned OFF mid-session
    ],
)
def test_session_restore_survives_lever_changes(tmp_path, first, second):
    """Two turns of one session through a real Strands session manager, no model calls."""
    from strands import Agent
    from strands.session.file_session_manager import FileSessionManager

    def turn(flag_kwargs):
        flags = _flags(**flag_kwargs)
        model = _orch_model(flags)
        built = build_conversation_manager(flags, model)
        return Agent(
            model=model,
            agent_id="orchestrator",
            callback_handler=None,
            conversation_manager=built.manager,
            session_manager=FileSessionManager(session_id="s1", storage_dir=str(tmp_path)),
        )

    turn(first)  # persists this manager's class + state
    turn(second)  # used to raise ValueError("Invalid conversation manager state.")


def test_reconcile_keeps_offset_and_summary():
    summary = {"role": "user", "content": [{"text": "Summary"}]}
    legacy = {
        "__name__": "SummarizingConversationManager",
        "removed_message_count": 6,
        "summary_message": summary,
    }
    summ = SessionSummarizingConversationManager()
    assert summ.restore_from_session(legacy) == [summary]
    assert summ.removed_message_count == 6

    slide = SessionSlidingWindowConversationManager()
    slide.restore_from_session(legacy)
    assert slide.removed_message_count == 6  # offset kept so restore stays aligned


# ---- observation masking ------------------------------------------------------


def _history():
    return [
        {"role": "user", "content": [{"text": "Where is my order?"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "Let me check."},
                {
                    "toolUse": {
                        "toolUseId": "t1",
                        "name": "ask_order",
                        "input": {"task": "latest order status"},
                    }
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"text": "x" * 2000}]}},
            ],
        },
        {"role": "assistant", "content": [{"text": "It ships tomorrow."}]},
    ]


def test_masking_preserves_count_and_roles():
    msgs = _history()
    roles = [m["role"] for m in msgs]
    assert mask_restored_tool_history(msgs) == 2
    assert len(msgs) == 4 and [m["role"] for m in msgs] == roles
    flat = json.dumps(msgs)
    assert "toolUse" not in flat and "toolResult" not in flat and "x" * 100 not in flat
    assert msgs[1]["content"][1] == {"text": "[earlier call: ask_order(latest order status)]"}
    assert msgs[3]["content"] == [{"text": "It ships tomorrow."}]  # final reply kept


def test_masking_is_deterministic():
    a, b = _history(), _history()
    mask_restored_tool_history(a)
    mask_restored_tool_history(b)
    assert a == b  # byte-stable masked prefix -> cache-friendly


def test_masking_on_by_default_when_enabled():
    flags = _flags(cache=True)
    assert build_conversation_manager(flags, _orch_model(flags)).mask_tool_history is True


# ---- chat API surfaces agent errors ---------------------------------------------


def test_chat_api_maps_agent_error_to_502(monkeypatch):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "app-plane", "chat_api", "handler.py")
    spec = importlib.util.spec_from_file_location("chat_handler", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    body = {"reply": "", "error": "ValueError: Invalid conversation manager state."}
    fake = SimpleNamespace(
        invoke_agent_runtime=lambda **kw: {
            "response": SimpleNamespace(read=lambda: json.dumps(body).encode())
        }
    )
    monkeypatch.setattr(mod, "_RUNTIME_ARN", "arn:test")
    monkeypatch.setattr(mod, "_client", lambda: fake)
    event = {
        "httpMethod": "POST",
        "headers": {"Authorization": "Bearer id.token.jwt"},
        "body": json.dumps({"message": "hi", "conversation_id": "c1"}),
        "requestContext": {"authorizer": {"claims": {"sub": "user-" + "0" * 32, "email": "a@b.c"}}},
    }
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 502
    assert "Invalid conversation manager state" in json.loads(resp["body"])["error"]


def test_per_agent_breakdown_has_no_summarizer():
    m = _load_metrics()
    usage = {
        "by_agent": {"orchestrator": {"cost_usd": 0.05}},
        "by_component": {"summarizer": {"cost_usd": 0.004}},
    }
    assert set(m.per_agent([{"turn": 0, "usage": usage}])) == {"orchestrator"}


def test_component_keeps_agent_model_for_cache_pricing():
    c = UsageCollector()
    c.record_usage("global.anthropic.claude-sonnet-5-5", {"inputTokens": 10}, agent="orchestrator")
    c.record_usage(
        "us.anthropic.claude-haiku-5-5",
        {"inputTokens": 10},
        agent="orchestrator",
        component="summarizer",
    )
    assert c.to_dict()["by_agent"]["orchestrator"]["model_id"].startswith("global.anthropic.claude-sonnet")
