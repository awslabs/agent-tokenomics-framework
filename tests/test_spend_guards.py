"""Unit tests for the T5 / T6 / T7 fixes (offline, no AWS).

  - T7: usage-plan reads fail closed (last good config, else FAIL_SAFE caps).
  - T6: message / conversation-id size limits, and tool-call caps on the
        specialists, not just the orchestrator.
  - T5: the orchestrator (and its routing candidates) carry the support
        Bedrock Guardrail; the specialists don't see raw customer text.

Run with the app-plane venv (needs strands):
    src/app-plane/.venv/bin/python -m pytest tests/test_spend_guards.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("strands")

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "src" / "app-plane"
for _p in (_ROOT / "src", _APP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _var in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
    os.environ.setdefault(_var, f"test-{_var.lower()}")


@pytest.fixture
def app(monkeypatch):
    """The app-plane modules, imported with the app plane first on sys.path.

    The control-plane tests also own a top-level `config` module, so drop any
    cached one and import fresh (monkeypatch restores sys.modules afterwards).
    """
    monkeypatch.syspath_prepend(str(_ROOT / "src"))
    monkeypatch.syspath_prepend(str(_APP))
    for name in list(sys.modules):
        if name in ("config", "tokenomics", "agents", "tools") or name.startswith(
            ("tokenomics.", "agents.", "tools.")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    import config
    from tokenomics import factory, plans

    assert Path(config.__file__).parent == _APP
    return SimpleNamespace(config=config, plans=plans, factory=factory)


# ---- T7: governance fails closed --------------------------------------------


class _Table:
    def __init__(self, result):
        self.result = result

    def scan(self):
        if isinstance(self.result, Exception):
            raise self.result
        return {"Items": self.result}


def _use_table(app, monkeypatch, result):
    monkeypatch.setattr(app.config, "USAGE_PLANS_TABLE", "plans")
    monkeypatch.setattr(app.plans, "_plans_table", lambda: _Table(result))


def test_read_failure_with_no_history_uses_fail_safe(app, monkeypatch):
    monkeypatch.setattr(app.plans, "_last_good", None)
    _use_table(app, monkeypatch, RuntimeError("ProvisionedThroughputExceededException"))
    cfg = app.plans._read_table()
    assert cfg is app.plans.FAIL_SAFE
    assert cfg.quota_for("bob").daily_max_tokens_per_user == 200000  # caps still on
    assert cfg.agent_limits("order").tool_calls_limit() == 5


def test_read_failure_keeps_last_good_config(app, monkeypatch):
    monkeypatch.setattr(app.plans, "_last_good", None)
    good = [{"plan_id": "user#default", "config": {"daily_max_tokens_per_user": 1234}}]
    _use_table(app, monkeypatch, good)
    assert app.plans._read_table().quota_for("bob").daily_max_tokens_per_user == 1234

    _use_table(app, monkeypatch, RuntimeError("boom"))
    assert app.plans._read_table().quota_for("bob").daily_max_tokens_per_user == 1234


@pytest.mark.parametrize(
    "result",
    [[], [{"plan_id": "user#default", "config": {"daily_max_tokens_per_user": "not-a-number"}}]],
    ids=["empty-table", "malformed-row"],
)
def test_empty_or_malformed_config_fails_closed(app, monkeypatch, result):
    monkeypatch.setattr(app.plans, "_last_good", None)
    _use_table(app, monkeypatch, result)
    assert app.plans._read_table() is app.plans.FAIL_SAFE


def test_unset_table_means_no_caps_for_local_dev(app, monkeypatch):
    monkeypatch.setattr(app.config, "USAGE_PLANS_TABLE", "")
    cfg = app.plans._read_table()
    assert cfg.quota_for("bob").daily_max_tokens_per_user in (0, None)


# ---- T6: tool-call caps on every agent --------------------------------------


@pytest.fixture
def tool_caps(app, monkeypatch):
    """Record every ToolCapHook the factory builds: {agent_name: max_calls}."""
    built: dict[str, int] = {}

    class Recording(app.factory.ToolCapHook):
        def __init__(self, max_calls, outcome, agent_name="orchestrator"):
            super().__init__(max_calls, outcome, agent_name)
            built[agent_name] = self.max_calls

    monkeypatch.setattr(app.factory, "ToolCapHook", Recording)
    return built


def test_specialists_get_their_own_tool_caps(app, tool_caps):
    from shared.flags_schema import FlagConfig
    from tokenomics.governance_outcome import GovernanceOutcome

    app.factory.build_specialists(FlagConfig.from_dict({}), app.plans.FAIL_SAFE, outcome=GovernanceOutcome())
    assert tool_caps == {"knowledge": 4, "order": 5}


def test_no_cap_configured_means_no_hook(app, tool_caps):
    from shared.flags_schema import FlagConfig
    from shared.usage_plans_schema import GovernanceConfig
    from tokenomics.governance_outcome import GovernanceOutcome

    app.factory.build_specialists(
        FlagConfig.from_dict({}), GovernanceConfig.empty(), outcome=GovernanceOutcome()
    )
    assert tool_caps == {}


def test_specialist_tool_cap_cancels_calls_past_the_limit(app):
    from tokenomics.governance_outcome import GovernanceOutcome

    outcome = GovernanceOutcome()
    hook = app.factory.ToolCapHook(2, outcome, agent_name="order")
    events = [SimpleNamespace(tool_use={"name": "get_orders"}, cancel_tool=False) for _ in range(3)]
    for e in events:
        hook._check(e)
    assert [bool(e.cancel_tool) for e in events] == [False, False, True]


# ---- T6: input size limits ---------------------------------------------------


def _chat_handler(monkeypatch):
    spec = importlib.util.spec_from_file_location("chat_handler_limits", _APP / "chat_api" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod, "_RUNTIME_ARN", "arn:test")
    monkeypatch.setattr(
        mod,
        "_client",
        lambda: SimpleNamespace(
            invoke_agent_runtime=lambda **kw: (
                calls.append(kw) or {"response": SimpleNamespace(read=lambda: b'{"reply": "ok"}')}
            )
        ),
    )
    return mod, calls


def _event(body):
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": "Bearer t"},
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {"sub": "0b7e4c2a-1111-2222-3333-444455556666"}}},
    }


@pytest.mark.parametrize(
    "body",
    [{"message": "x" * 4001}, {"message": "hi", "conversation_id": "c" * 65}],
    ids=["message", "conversation_id"],
)
def test_chat_proxy_rejects_oversized_input_before_invoking(monkeypatch, body):
    mod, calls = _chat_handler(monkeypatch)
    assert mod.handler(_event(body), None)["statusCode"] == 400
    assert calls == []  # never reached the runtime / Bedrock


def test_chat_proxy_accepts_message_at_the_limit(monkeypatch):
    mod, calls = _chat_handler(monkeypatch)
    assert mod.handler(_event({"message": "x" * 4000}), None)["statusCode"] == 200
    assert len(calls) == 1


# ---- T5: guardrail on the customer-facing model ------------------------------


def _guardrail_of(model):
    return model.config.get("guardrail_id"), model.config.get("guardrail_latest_message")


def test_orchestrator_model_carries_the_guardrail(app, monkeypatch):
    from shared.flags_schema import FlagConfig

    monkeypatch.setattr(app.config, "SUPPORT_GUARDRAIL_ID", "gr-123")
    monkeypatch.setattr(app.config, "SUPPORT_GUARDRAIL_VERSION", "1")
    flags = FlagConfig.from_dict({})
    guarded = app.factory.build_model("us.anthropic.claude-haiku-5-5", flags, guardrail=True)
    plain = app.factory.build_model("us.anthropic.claude-haiku-5-5", flags)
    assert _guardrail_of(guarded) == ("gr-123", True)
    assert _guardrail_of(plain) == (None, None)


def test_routing_candidates_are_guarded_for_the_orchestrator_only(app, monkeypatch):
    from shared.flags_schema import FlagConfig

    monkeypatch.setattr(app.config, "SUPPORT_GUARDRAIL_ID", "gr-123")
    monkeypatch.setattr(app.config, "SUPPORT_GUARDRAIL_VERSION", "1")
    flags = FlagConfig.from_dict({"model_routing": {"enabled": True}})

    def candidate_guardrails(role):
        router = app.factory.build_router(role, flags).router
        return {_guardrail_of(c.model)[0] for c in router.candidates}

    assert candidate_guardrails("orchestrator") == {"gr-123"}
    assert candidate_guardrails("knowledge") == {None}


def test_no_guardrail_configured_builds_plain_model(app, monkeypatch):
    from shared.flags_schema import FlagConfig

    monkeypatch.setattr(app.config, "SUPPORT_GUARDRAIL_ID", "")
    model = app.factory.build_model("us.anthropic.claude-haiku-5-5", FlagConfig.from_dict({}), guardrail=True)
    assert _guardrail_of(model) == (None, None)
