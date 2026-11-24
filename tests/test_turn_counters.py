"""Unit tests for T6 / T24: every turn that spent tokens advances the quota counters.

  - normal completion advances the session / day / month / minute counters;
  - an output-cap cutoff (MaxTokensReachedException) advances them too;
  - any other failure after model calls started advances them, emits the usage
    event, and re-raises the ORIGINAL error;
  - the pre-turn session-cap stop (no model calls) advances nothing.

Drives the real `handle_turn` with a stubbed Strands Agent (offline, no AWS).

Run with the app-plane venv (needs strands):
    src/app-plane/.venv/bin/python -m pytest tests/test_turn_counters.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("strands")

from strands.types.exceptions import MaxTokensReachedException

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "src" / "app-plane"
for _p in (_ROOT / "src", _APP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _var in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
    os.environ.setdefault(_var, f"test-{_var.lower()}")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Orchestrator usage the fake agent reports: on a completed call (AgentResult
# metrics) and on a failed / cut-off call (event-loop metrics, recovered).
_DONE = {"inputTokens": 100, "outputTokens": 20}  # 120 total
_PARTIAL = {"inputTokens": 60, "outputTokens": 8}  # 68 total
_SPECIALIST = {"inputTokens": 1000, "outputTokens": 200}  # 1200 total


@pytest.fixture
def turn(monkeypatch):
    """Import the app-plane fresh and stub every external side effect.

    Returns a namespace with `run(fake_agent_cls)`, the recorded counter writes
    (`writes`) and emitted usage events (`events`).
    """
    monkeypatch.syspath_prepend(str(_ROOT / "src"))
    monkeypatch.syspath_prepend(str(_APP))
    for name in list(sys.modules):
        if name in ("config", "tokenomics", "agents", "tools", "delegation") or name.startswith(
            ("tokenomics.", "agents.", "tools.")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)

    import agents.orchestrator.agent as agent_mod
    from agents.orchestrator import turn_results
    from shared.flags_schema import FlagConfig
    from shared.usage_plans_schema import GovernanceConfig, UserQuota
    from tokenomics import flags as flags_mod
    from tokenomics import plans as plans_mod

    writes: list[tuple[str, int]] = []
    events: list[dict] = []
    for fn in ("add_day", "add_month", "add_minute"):
        monkeypatch.setattr(turn_results.quota_counter, fn, lambda _u, t, _fn=fn: writes.append((_fn, t)))
    monkeypatch.setattr(
        turn_results.session_counter, "add_session_tokens", lambda _s, t: writes.append(("session", t))
    )
    monkeypatch.setattr(turn_results.metering, "emit_usage", lambda **kw: events.append(kw))
    monkeypatch.setattr(turn_results.metering, "emit_governance_event", lambda **kw: None)

    # Capture the turn's UsageCollector so a fake agent can simulate a specialist
    # recording its spend before the orchestrator fails.
    real_tools = agent_mod.make_specialist_tools
    seen: dict = {}

    def _tools(**kw):
        seen["collector"] = kw["collector"]
        return real_tools(**kw)

    monkeypatch.setattr(agent_mod, "make_specialist_tools", _tools)

    def run(fake_cls, *, session_cap: int = 0):
        monkeypatch.setattr(agent_mod, "Agent", fake_cls)
        flags_mod.set_override(FlagConfig.from_dict({}))
        plans_mod.set_override(
            GovernanceConfig(
                user_default=UserQuota(
                    daily_max_tokens_per_user=200_000,
                    monthly_max_tokens_per_user=4_000_000,
                    max_tokens_per_minute_per_user=40_000,
                ),
                session_max_tokens=session_cap,
            )
        )
        try:
            ctx = agent_mod.RunContext(user_id="bob", profile={"name": "Bob"}, session_id="s1")
            return agent_mod.handle_turn("Write me a very long answer", ctx)
        finally:
            flags_mod.set_override(None)
            plans_mod.set_override(None)

    return SimpleNamespace(run=run, writes=writes, events=events, seen=seen, agent_mod=agent_mod)


def _fake_agent(*, result=None, raises=None, specialist_spend=False, seen=None):
    class _FakeAgent:
        def __init__(self, **kwargs):
            self.messages = []
            self.event_loop_metrics = SimpleNamespace(accumulated_usage=dict(_PARTIAL))

        def __call__(self, message=None, **kw):
            if specialist_spend:
                seen["collector"].record_usage("specialist-model", dict(_SPECIALIST), agent="knowledge")
            if raises is not None:
                raise raises
            return result

    return _FakeAgent


def _quota(total):
    return [("add_day", total), ("add_month", total), ("add_minute", total)]


def test_normal_turn_advances_every_counter_once(turn):
    result = SimpleNamespace(stop_reason="end_turn", metrics=SimpleNamespace(accumulated_usage=dict(_DONE)))
    turn.run(_fake_agent(result=result), session_cap=1_000_000)
    assert sorted(turn.writes) == sorted([("session", 120), *_quota(120)])
    assert len(turn.events) == 1


def test_output_cap_cutoff_advances_counters(turn):
    # T24 / finding 1: the cut-off turn is billed, so the limits must move too.
    tr = turn.run(_fake_agent(raises=MaxTokensReachedException("cap")), session_cap=1_000_000)
    assert tr.governance["reason"] == "limit_output_tokens"
    assert sorted(turn.writes) == sorted([("session", 68), *_quota(68)])
    assert len(turn.events) == 1  # exactly one usage event, no double count


def test_failed_turn_counts_spend_and_reraises_the_original_error(turn):
    # T24 / finding A1: a failure after model calls must not make the spend free.
    boom = RuntimeError("bedrock exploded")
    with pytest.raises(RuntimeError) as exc:
        turn.run(_fake_agent(raises=boom), session_cap=1_000_000)
    assert exc.value is boom
    assert sorted(turn.writes) == sorted([("session", 68), *_quota(68)])
    assert len(turn.events) == 1
    assert turn.events[0]["usage"]["output_tokens"] == 8


def test_failed_turn_includes_specialist_spend_already_recorded(turn):
    # A specialist finished (and recorded its usage) before the orchestrator failed.
    agent = _fake_agent(raises=RuntimeError("tool timeout"), specialist_spend=True, seen=turn.seen)
    with pytest.raises(RuntimeError):
        turn.run(agent)
    assert sorted(turn.writes) == sorted(_quota(68 + 1200))
    assert "session" not in dict(turn.writes)  # no session cap configured -> no session write


def test_counting_failure_never_masks_the_turn_error(turn, monkeypatch):
    def _broken(*_a, **_k):
        raise ConnectionError("dynamodb down")

    monkeypatch.setattr(turn.agent_mod, "advance_counters", _broken)
    boom = RuntimeError("original")
    with pytest.raises(RuntimeError) as exc:
        turn.run(_fake_agent(raises=boom))
    assert exc.value is boom


def test_session_cap_stop_spends_nothing_and_counts_nothing(turn, monkeypatch):
    from tokenomics import session_counter

    monkeypatch.setattr(session_counter, "session_tokens", lambda _s: 5_000)

    class _NeverCalled:
        def __init__(self, **kwargs):
            raise AssertionError("no agent should be built after the session cap stops the turn")

    tr = turn.run(_NeverCalled, session_cap=5_000)
    assert tr.governance["reason"] == "session_budget"
    assert turn.writes == []
