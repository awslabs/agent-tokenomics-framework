"""Unit tests for the core helpers the cleanup introduced (no AWS).

These cover code the governance suite skips: the quota/session hooks there
monkeypatch `quota_counter.read_*`, which bypasses the shared counter store and
the turn-wrap-up helpers entirely. So without these tests, flipping
`read_tokens(fail_closed=...)`, dropping the `advance_counters` gating, or
removing the `outcome.acted()` gate in `finalize_turn` would still pass the
suite. Each test here exercises the REAL path and asserts the behavioural
difference, so those regressions fail.

Covered:
  * shared/dynamo.py        -- to_dynamo / to_plain Decimal<->int/float round-trip
  * _counter_store.read_tokens -- fail-CLOSED raises vs fail-OPEN returns 0
  * turn_results.advance_counters -- the per-dimension counter gating
  * turn_results.finalize_turn    -- the outcome.acted() gate on the gov event

Run:
    src/app-plane/.venv/bin/python -m pytest tests/test_core_helpers.py -q
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "src" / "app-plane"))


# ---- shared/dynamo: the float <-> Decimal boundary -------------------------


def test_to_dynamo_floats_become_decimal_recursively():
    from shared.dynamo import to_dynamo

    out = to_dynamo({"a": 1.5, "b": [2.0, {"c": 0.1}], "d": 3, "e": "x"})
    assert isinstance(out["a"], Decimal) and out["a"] == Decimal("1.5")
    assert isinstance(out["b"][0], Decimal) and out["b"][0] == Decimal("2.0")
    assert isinstance(out["b"][1]["c"], Decimal)
    # non-floats pass through untouched (int stays int, str stays str)
    assert out["d"] == 3 and not isinstance(out["d"], Decimal)
    assert out["e"] == "x"


def test_to_dynamo_uses_str_not_binary_float():
    """Decimal(str(f)) avoids the binary-float artefact Decimal(float) would add."""
    from shared.dynamo import to_dynamo

    # Decimal(0.1) == Decimal('0.1000000000000000055...'); Decimal(str(0.1)) == 0.1.
    assert to_dynamo(0.1) == Decimal("0.1")


def test_to_plain_integral_decimal_is_int_fractional_is_float():
    from shared.dynamo import to_plain

    assert to_plain(Decimal("2")) == 2 and isinstance(to_plain(Decimal("2")), int)
    v = to_plain(Decimal("2.5"))
    assert v == 2.5 and isinstance(v, float)


def test_to_plain_recurses_through_dict_and_list():
    from shared.dynamo import to_plain

    out = to_plain({"n": Decimal("10"), "xs": [Decimal("1.5"), Decimal("3")], "s": "keep"})
    assert out["n"] == 10 and isinstance(out["n"], int)
    assert out["xs"] == [1.5, 3] and isinstance(out["xs"][0], float) and isinstance(out["xs"][1], int)
    assert out["s"] == "keep"


def test_dynamo_roundtrip_preserves_values_and_types():
    from shared.dynamo import to_dynamo, to_plain

    original = {"cost_usd": 0.011369, "tokens": 3981, "nested": {"ratio": 0.5, "count": 2}}
    rt = to_plain(to_dynamo(original))
    assert rt == original
    assert isinstance(rt["tokens"], int) and isinstance(rt["cost_usd"], float)
    assert isinstance(rt["nested"]["count"], int) and isinstance(rt["nested"]["ratio"], float)


# ---- _counter_store.read_tokens: fail-closed vs fail-open ------------------


def _counter_store(monkeypatch, *, item=None, raise_read=False):
    """Import _counter_store with its DynamoDB table stubbed at the boundary.

    Stubbing `table()` (not the quota_counter wrapper) means the real
    read_tokens branch runs -- so the fail_closed flag is actually exercised.
    """
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("ENFORCEMENT_COUNTER_TABLE", "counter-table")
    from tokenomics import _counter_store as cs

    class _Table:
        def get_item(self, **_kwargs):  # boto3 calls with Key=...
            if raise_read:
                raise RuntimeError("dynamodb down")
            return {"Item": item} if item is not None else {}

    monkeypatch.setattr(cs, "table", lambda: _Table())
    return cs


def test_read_tokens_missing_row_is_zero(monkeypatch):
    cs = _counter_store(monkeypatch, item=None)
    assert cs.read_tokens("USER#bob#DAY#x", fail_closed=True) == 0
    assert cs.read_tokens("SESSION#s1", fail_closed=False) == 0


def test_read_tokens_returns_stored_total(monkeypatch):
    cs = _counter_store(monkeypatch, item={"tokens": Decimal("1500")})
    assert cs.read_tokens("USER#bob#DAY#x", fail_closed=True) == 1500


def test_read_tokens_fail_closed_raises_on_store_error(monkeypatch):
    """A money cap must NOT under-enforce: a read error raises so the caller blocks."""
    cs = _counter_store(monkeypatch, raise_read=True)
    with pytest.raises(cs.CounterUnavailable):
        cs.read_tokens("USER#bob#DAY#x", fail_closed=True)


def test_read_tokens_fail_open_returns_zero_on_store_error(monkeypatch):
    """A UX guard degrades to baseline: a read error returns 0 (cap skipped)."""
    cs = _counter_store(monkeypatch, raise_read=True)
    assert cs.read_tokens("SESSION#s1", fail_closed=False) == 0


def test_quota_counter_is_fail_closed_session_counter_is_fail_open(monkeypatch):
    """The two wrappers wire the policy the comment can regress: quota raises, session returns 0."""
    cs = _counter_store(monkeypatch, raise_read=True)
    from tokenomics import quota_counter as qc
    from tokenomics import session_counter as sc

    # both wrappers call the SAME stubbed store; only the fail policy differs
    monkeypatch.setattr(qc, "_counter_store", cs)
    monkeypatch.setattr(sc, "_counter_store", cs)
    with pytest.raises(qc.CounterUnavailable):
        qc.read_day("bob")  # fail-CLOSED
    assert sc.session_tokens("s1") == 0  # fail-OPEN


# ---- turn_results.advance_counters: per-dimension gating -------------------


def _advance_env(monkeypatch):
    """Capture every counter increment advance_counters makes, without AWS."""
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    from agents.orchestrator import turn_results as tr

    calls: dict[str, int] = {}
    monkeypatch.setattr(tr.session_counter, "add_session_tokens", lambda sid, t: calls.__setitem__("session", t))
    monkeypatch.setattr(tr.quota_counter, "add_day", lambda u, t: calls.__setitem__("day", t))
    monkeypatch.setattr(tr.quota_counter, "add_month", lambda u, t: calls.__setitem__("month", t))
    monkeypatch.setattr(tr.quota_counter, "add_minute", lambda u, t: calls.__setitem__("minute", t))
    return tr, calls


def _ctx(user_id="bob"):
    from tokenomics.identity import RunContext

    return RunContext(user_id=user_id, session_id="s1")


def _gov(**quota):
    from shared.usage_plans_schema import GovernanceConfig, UserQuota

    return GovernanceConfig(user_default=UserQuota(**quota)) if quota else GovernanceConfig.empty()


def test_advance_counters_no_write_for_zero_total(monkeypatch):
    tr, calls = _advance_env(monkeypatch)
    tr.advance_counters(_ctx(), _gov(daily_max_tokens_per_user=100), session_cap=1000, turn_total=0)
    assert calls == {}


def test_advance_counters_session_only_when_cap_configured(monkeypatch):
    tr, calls = _advance_env(monkeypatch)
    # session_cap = 0 -> no session write even though there are tokens
    tr.advance_counters(_ctx(), _gov(), session_cap=0, turn_total=500)
    assert "session" not in calls
    tr.advance_counters(_ctx(), _gov(), session_cap=1000, turn_total=500)
    assert calls["session"] == 500


def test_advance_counters_only_the_quota_dimensions_the_user_has(monkeypatch):
    tr, calls = _advance_env(monkeypatch)
    # only daily + minute configured -> month must NOT be written
    gov = _gov(daily_max_tokens_per_user=100, max_tokens_per_minute_per_user=50)
    tr.advance_counters(_ctx(), gov, session_cap=0, turn_total=300)
    assert calls == {"day": 300, "minute": 300}
    assert "month" not in calls


def test_advance_counters_skips_quota_when_no_user(monkeypatch):
    tr, calls = _advance_env(monkeypatch)
    tr.advance_counters(_ctx(user_id=""), _gov(daily_max_tokens_per_user=100), session_cap=0, turn_total=300)
    assert calls == {}


# ---- turn_results.finalize_turn: the outcome.acted() gate ------------------


def _finalize_env(monkeypatch):
    """Capture metering emissions so we can assert the acted() gate."""
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    from agents.orchestrator import turn_results as tr

    usage_emits: list = []
    gov_emits: list = []
    monkeypatch.setattr(tr.metering, "emit_usage", lambda **kw: usage_emits.append(kw))
    monkeypatch.setattr(tr.metering, "emit_governance_event", lambda **kw: gov_emits.append(kw))
    return tr, usage_emits, gov_emits


def test_finalize_turn_always_emits_usage_and_builds_result(monkeypatch):
    from tokenomics.governance_outcome import GovernanceOutcome

    tr, usage_emits, gov_emits = _finalize_env(monkeypatch)
    res = tr.finalize_turn(
        _ctx(),
        reply="hello",
        usage={"cost_usd": 0.01},
        model_ids={"orchestrator": "m"},
        outcome=GovernanceOutcome(),  # nothing acted
    )
    assert len(usage_emits) == 1  # usage is ALWAYS emitted
    assert gov_emits == []  # but no governance event when nothing acted
    assert res.reply == "hello"
    assert res.usage == {"cost_usd": 0.01}
    assert res.governance["reason"] is None
    assert res.identity["session_id"] == "s1"


def test_finalize_turn_emits_governance_event_only_when_acted(monkeypatch):
    from tokenomics.governance_outcome import GovernanceOutcome

    tr, usage_emits, gov_emits = _finalize_env(monkeypatch)
    oc = GovernanceOutcome()
    oc.record(stopped=True, reason="quota", scope="user", config="daily_max_tokens_per_user")
    assert oc.acted()
    tr.finalize_turn(
        _ctx(), reply="blocked", usage={"cost_usd": 0.0}, model_ids={}, outcome=oc
    )
    assert len(usage_emits) == 1
    assert len(gov_emits) == 1  # the acted() gate opened
    assert gov_emits[0]["governance"]["reason"] == "quota"
