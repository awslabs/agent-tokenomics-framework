"""Unit tests for token-governance pure logic (no AWS).

Run from the repo root:
    src/app-plane/.venv/bin/python -m pytest tests/test_token_governance.py -q
(the app-plane venv has strands + boto3 for the hook tests).

Tests are grouped so the schema/consumer/analytics tests run under plain python;
the Strands-hook tests skip cleanly if strands is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "app-plane"))
sys.path.insert(0, str(_SRC / "control-plane"))


# ---- shared schema ---------------------------------------------------------


def test_governance_config_roundtrip_and_accessors():
    from shared.usage_plans_schema import AgentLimits, GovernanceConfig, UserQuota

    cfg = GovernanceConfig(
        agents={
            "orchestrator": AgentLimits(
                max_iterations=8,
                max_tokens_per_agent_call=60000,
                max_tool_calls=6,
                max_output_tokens_per_model_call=2000,
            )
        },
        session_max_tokens=150000,
        user_default=UserQuota(daily_max_tokens_per_user=200000, max_tokens_per_minute_per_user=40000),
    )
    rt = GovernanceConfig.from_items(cfg.to_items())
    o = rt.agent_limits("orchestrator")
    assert o.turns_limit() == 8
    assert o.total_tokens_limit() == 60000
    assert o.tool_calls_limit() == 6
    assert o.output_tokens_cap() == 2000
    assert rt.session_max_tokens == 150000  # co-located on user#default row
    assert rt.agent_limits("knowledge").turns_limit() is None  # no cap
    assert rt.quota_for("anyone").daily_max_tokens_per_user == 200000  # global default


def test_governance_config_from_dict_roundtrip():
    from shared.usage_plans_schema import AgentLimits, GovernanceConfig

    cfg = GovernanceConfig(agents={"order": AgentLimits(max_tool_calls=4)}, session_max_tokens=1000)
    rt = GovernanceConfig.from_dict(cfg.to_dict())
    assert rt.session_max_tokens == 1000
    assert rt.agent_limits("order").tool_calls_limit() == 4


def test_empty_is_no_caps():
    from shared.usage_plans_schema import GovernanceConfig

    e = GovernanceConfig.empty()
    assert e.agent_limits("orchestrator").turns_limit() is None
    assert e.agent_limits("orchestrator").output_tokens_cap() is None
    assert e.session_max_tokens == 0
    assert e.quota_for("x").daily_max_tokens_per_user == 0


def test_zero_value_means_no_cap():
    from shared.usage_plans_schema import AgentLimits

    z = AgentLimits(max_iterations=0, max_tokens_per_agent_call=0)
    assert z.turns_limit() is None and z.total_tokens_limit() is None


# ---- price map (cost math) -------------------------------------------------


def test_cost_usd_prices_buckets_separately():
    from shared.price_map import cost_usd, known_model_ids

    mid = known_model_ids()[0]
    all_input = cost_usd(mid, input_tokens=1000)
    with_cache = cost_usd(mid, input_tokens=0, cache_read_tokens=1000)
    assert with_cache < all_input
    assert cost_usd(mid) == 0.0


# ---- governance outcome (in-loop) ------------------------------------------


def test_outcome_stop_reason_maps_to_config():
    from tokenomics.governance_outcome import GovernanceOutcome

    o = GovernanceOutcome()
    o.note_stop_reason("end_turn")
    assert not o.acted()  # normal stop -> nothing

    o2 = GovernanceOutcome()
    o2.note_stop_reason("limit_turns")
    assert o2.stopped and o2.reason == "limit_turns"
    assert o2.to_dict()["config"] == "max_iterations"

    o3 = GovernanceOutcome()
    o3.note_stop_reason("limit_total_tokens")
    assert o3.to_dict()["config"] == "max_tokens_per_agent_call"

    o4 = GovernanceOutcome()
    o4.note_stop_reason("max_tokens")
    assert o4.limited and not o4.stopped
    assert o4.to_dict()["config"] == "max_output_tokens_per_model_call"


# ---- delegation _limits_for (value-gated, no enabled flag) -----------------


def test_limits_for_gates_on_value():
    # The Strands-Limits projection lives on AgentLimits itself (one source,
    # used by both the orchestrator turn and specialist delegation).
    from shared.usage_plans_schema import AgentLimits

    assert AgentLimits().as_strands_limits() is None
    assert AgentLimits(max_iterations=5, max_tokens_per_agent_call=1000).as_strands_limits() == {
        "turns": 5,
        "total_tokens": 1000,
    }


# ---- metering consumer (routing + rollups) ---------------------------------


def test_consumer_routes_and_rolls_up(monkeypatch):
    import types

    adds: list = []
    counts: list = []
    events: list = []
    store = types.ModuleType("lib.store")
    store.add_metric_usage = lambda pk, t, c, extra=None, turns=0: adds.append(
        (pk, t, (extra or {}).get("user_id"), (extra or {}).get("source"), turns)
    )
    store.add_metric_count = lambda pk, n=1, extra=None: counts.append((pk, n))
    store.append_event = lambda e: events.append(e)
    # Advisor metering additions (mirror lib.store): token-split buckets + turn ledger.
    store.SPLIT_ATTRS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    store.append_turn = lambda session_id, entry, extra=None: True
    libpkg = types.ModuleType("lib")
    libpkg.store = store
    monkeypatch.setitem(sys.modules, "lib", libpkg)
    monkeypatch.setitem(sys.modules, "lib.store", store)

    import importlib

    mc = importlib.import_module("metering_consumer")
    importlib.reload(mc)

    # UsageRecorded -> session/user/agent rollups, tagged user_id
    usage_detail = {
        "user_id": "bob",
        "session_id": "s1",
        "ts": "2026-09-25T10:00:00+00:00",
        "usage": {
            "total_input_tokens": 1000,
            "output_tokens": 200,
            "cost_usd": 0.01,
            "by_agent": {
                "orchestrator": {"total_input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.01}
            },
        },
    }
    assert mc.handler({"detail-type": "UsageRecorded", "detail": usage_detail}, None)["ok"]
    pks = {a[0] for a in adds}
    assert (
        "SESSION#s1" in pks
        and "USER#bob#DAY#2026-09-25" in pks
        and "AGENT#orchestrator#USER#bob#DAY#2026-09-25" in pks
    )
    assert dict((a[0], a[1]) for a in adds)["SESSION#s1"] == 1200
    # live turns are tagged source=live and increment a per-turn counter on SESSION/USER rows
    live = {a[0]: a for a in adds}
    assert live["SESSION#s1"][3] == "live" and live["SESSION#s1"][4] == 1
    assert live["USER#bob#DAY#2026-09-25"][4] == 1

    # Benchmark turn (run_id present) -> BENCH# rows ONLY; never pollutes live USER#/SESSION#.
    adds.clear()
    bench_detail = {
        "user_id": "synthetic-1",
        "session_id": "run7-0",
        "run_id": "run7",
        "ts": "2026-09-25T10:05:00+00:00",
        "usage": {
            "total_input_tokens": 500,
            "output_tokens": 100,
            "cost_usd": 0.005,
            "by_agent": {"order": {"total_input_tokens": 500, "output_tokens": 100, "cost_usd": 0.005}},
        },
    }
    assert mc.handler({"detail-type": "UsageRecorded", "detail": bench_detail}, None)["ok"]
    bench_pks = [a[0] for a in adds]
    assert bench_pks == ["BENCH#RUN#run7", "BENCH#RUN#run7#AGENT#order"]
    assert all(a[3] == "benchmark" for a in adds)
    assert not any(pk.startswith(("SESSION#", "USER#", "AGENT#")) for pk in bench_pks)

    # LimitViolation -> VIOLATION# count + an event-log row
    viol_detail = {
        "user_id": "bob",
        "session_id": "s1",
        "ts": "2026-09-25T10:01:00+00:00",
        "config": "max_iterations",
        "scope": "turn",
        "reason": "limit_turns",
        "detail": "cutoff",
        "stopped": True,
    }
    assert mc.handler({"detail-type": "LimitViolation", "detail": viol_detail}, None)["ok"]
    assert any(pk == "VIOLATION#max_iterations#DAY#2026-09-25" for pk, _ in counts)
    assert events and events[-1]["config"] == "max_iterations" and events[-1]["type"] == "limit_turns"

    # QuotaBlock (403) + ThrottleEvent (429)
    assert mc.handler(
        {"detail-type": "QuotaBlock", "detail": {"user_id": "bob", "config": "daily_max_tokens_per_user"}},
        None,
    )["ok"]
    assert mc.handler({"detail-type": "ThrottleEvent", "detail": {"user_id": "bob"}}, None)["ok"]
    assert any(pk.startswith("THROTTLE#bob#DAY#") for pk, _ in counts)


# ---- analytics handler (window math) ---------------------------------------


def test_analytics_window_days(monkeypatch):
    for k in ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE"):
        monkeypatch.setenv(k, "x")
    import handlers.governance as g

    assert len(g._window_days("24h")) == 1
    assert len(g._window_days("7d")) == 7
    assert len(g._window_days("30d")) == 30


def test_analytics_folds_legacy_summarizer_rows_into_orchestrator(monkeypatch):
    """Rows written before the summarizer became a component never show as an agent."""
    for k in ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE"):
        monkeypatch.setenv(k, "x")
    import handlers.governance as g
    from shared.usage_plans_schema import GovernanceConfig

    day = g._window_days("24h")[0]
    rows = [
        {"pk": f"AGENT#orchestrator#USER#u1#DAY#{day}", "user_id": "u1", "tokens": 1000, "cost_usd": 0.05},
        {"pk": f"AGENT#summarizer#USER#u1#DAY#{day}", "user_id": "u1", "tokens": 300, "cost_usd": 0.01},
        {"pk": f"AGENT#order#USER#u1#DAY#{day}", "user_id": "u1", "tokens": 200, "cost_usd": 0.02},
    ]
    monkeypatch.setattr(g.store, "list_metrics", lambda: rows)
    monkeypatch.setattr(g.store, "list_events", lambda: [])
    monkeypatch.setattr(g.store, "read_governance_config", lambda: GovernanceConfig.empty().to_dict())
    monkeypatch.setattr(g, "_sub_for_email", lambda user: None)
    monkeypatch.setattr(g, "_email_for_sub", lambda sub: sub)

    by_agent = {a["agent"]: a for a in g.analytics(window="24h")["cost_by_agent"]}
    assert set(by_agent) == {"orchestrator", "order"}
    assert by_agent["orchestrator"]["tokens"] == 1300
    assert by_agent["orchestrator"]["cost"] == pytest.approx(0.06)


def _analytics_with_orphan(monkeypatch):
    """Load the analytics handler with a store holding one resolvable user (u1)
    and one orphan sub (cust-x: has usage rows but no live Cognito user).

    Mirrors the real resolver asymmetry: `_email_for_sub` falls back to the raw
    sub for display, while `_sub_for_email` returns None when it can't resolve an
    identifier to a live user. `cust-x` is exactly the orphaned-metrics case seen
    live (subs like cust-*/omni-test-user left over from earlier runs).
    """
    for k in ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE"):
        monkeypatch.setenv(k, "x")
    import handlers.governance as g
    from shared.usage_plans_schema import GovernanceConfig

    day = g._window_days("24h")[0]
    rows = [
        {"pk": f"USER#u1#DAY#{day}", "user_id": "u1", "tokens": 1000, "cost_usd": 0.05},
        {"pk": f"USER#cust-x#DAY#{day}", "user_id": "cust-x", "tokens": 300, "cost_usd": 0.02},
    ]
    monkeypatch.setattr(g.store, "list_metrics", lambda: rows)
    monkeypatch.setattr(g.store, "list_events", lambda: [])
    monkeypatch.setattr(g.store, "read_governance_config", lambda: GovernanceConfig.empty().to_dict())
    # u1 resolves to a sub; cust-x (and any other id) genuinely doesn't exist
    # (None), so the filter uses it verbatim. A real Cognito error would instead
    # raise -- see test_analytics_errors_not_zero_on_cognito_failure.
    monkeypatch.setattr(g, "_email_for_sub", lambda sub: {"u1": "u1@example.com"}.get(sub, sub))
    monkeypatch.setattr(g, "_sub_if_exists", lambda email: {"u1@example.com": "u1"}.get(email))
    return g


def test_analytics_unknown_user_returns_its_own_slice_not_fleet(monkeypatch):
    """A per-user query for an unresolvable id must return THAT id's rows (or none),
    never the whole fleet. The bug: _sub_for_email -> None was treated as 'all users',
    so analytics('cust-x') returned everyone's totals."""
    g = _analytics_with_orphan(monkeypatch)
    fleet = g.analytics(user="all", window="24h")["kpis"]["total_tokens"]
    assert fleet == 1300  # u1 (1000) + orphan cust-x (300)

    orphan = g.analytics(user="cust-x", window="24h")["kpis"]["total_tokens"]
    assert orphan == 300  # its own slice, NOT the fleet total
    assert orphan != fleet

    # A genuinely unknown id resolves to an empty slice, not the fleet.
    ghost = g.analytics(user="ghost@example.com", window="24h")["kpis"]["total_tokens"]
    assert ghost == 0


def test_analytics_per_user_sums_to_all_with_orphan_subs(monkeypatch):
    """Offline guard for the live invariant test_per_user_sums_to_all: the per-user
    totals for every user the all-view lists must sum back to the all-users total,
    even when some of those users are orphaned subs with no live Cognito email."""
    g = _analytics_with_orphan(monkeypatch)
    all_a = g.analytics(user="all", window="24h")
    summed = sum(
        g.analytics(user=u["user"], window="24h")["kpis"]["total_tokens"] for u in all_a["top_users"]
    )
    assert summed == all_a["kpis"]["total_tokens"]


def test_analytics_errors_not_zero_on_cognito_failure(monkeypatch):
    """A transient Cognito failure resolving a REAL user must fail the request, not
    report zero usage. (Regression: _resolve_sub used to mask every error as
    'not found', so the filter fell back to the raw email and returned 0.)"""
    g = _analytics_with_orphan(monkeypatch)

    class _Throttled(Exception):
        pass

    def _boom(email):
        raise _Throttled("Rate exceeded")

    monkeypatch.setattr(g, "_sub_if_exists", _boom)
    with pytest.raises(_Throttled):
        g.analytics(user="u1@example.com", window="24h")


def test_sub_if_exists_distinguishes_not_found_from_errors(monkeypatch):
    """_sub_if_exists returns None ONLY for UserNotFoundException; any other
    Cognito error propagates (so callers fail loudly, not silently)."""
    import types

    for k in ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE"):
        monkeypatch.setenv(k, "x")
    import handlers.governance as g

    class _UserNotFound(Exception):
        pass

    class _Throttling(Exception):
        pass

    class _FakeIdp:
        exceptions = types.SimpleNamespace(UserNotFoundException=_UserNotFound)

        def __init__(self, behavior):
            self._behavior = behavior

        def admin_get_user(self, **_):
            return self._behavior()

    monkeypatch.setattr(g.config, "APP_USER_POOL_ID", "pool-123")

    # found -> sub
    monkeypatch.setattr(
        g, "_cognito", lambda: _FakeIdp(lambda: {"UserAttributes": [{"Name": "sub", "Value": "abc"}]})
    )
    assert g._sub_if_exists("bob@example.com") == "abc"

    # genuinely absent -> None
    def _raise_nf():
        raise _UserNotFound()

    monkeypatch.setattr(g, "_cognito", lambda: _FakeIdp(_raise_nf))
    assert g._sub_if_exists("ghost@example.com") is None

    # any other error -> propagates
    def _raise_throttle():
        raise _Throttling("Rate exceeded")

    monkeypatch.setattr(g, "_cognito", lambda: _FakeIdp(_raise_throttle))
    with pytest.raises(_Throttling):
        g._sub_if_exists("bob@example.com")


# ---- Strands ToolCapHook (needs strands) -----------------------------------


def test_tool_cap_hook_cancels_past_limit_with_config():
    pytest.importorskip("strands")
    from tokenomics.governance_outcome import GovernanceOutcome
    from tokenomics.hooks.tool_cap import ToolCapHook

    class FakeEvt:
        def __init__(self):
            self.tool_use = {"name": "ask_order"}
            self.cancel_tool = False

    oc = GovernanceOutcome()
    h = ToolCapHook(2, oc, "orchestrator")
    h._reset(None)
    e1, e2, e3 = FakeEvt(), FakeEvt(), FakeEvt()
    h._check(e1)
    h._check(e2)
    h._check(e3)
    assert e1.cancel_tool is False and e2.cancel_tool is False
    assert e3.cancel_tool  # third exceeds the cap of 2
    assert oc.limited and oc.reason == "tool_cap"
    assert oc.to_dict()["config"] == "max_tool_calls"


# ---- quota_counter key helpers (no AWS) ------------------------------------


def test_quota_counter_keys(monkeypatch):
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    from tokenomics import quota_counter as qc

    assert qc.day_key("bob").startswith("USER#bob#DAY#")
    assert qc.month_key("bob").startswith("USER#bob#MONTH#")
    assert qc.minute_key("bob").startswith("USER#bob#MIN#")


# ---- Strands QuotaHook: in-loop per-user quota + throttle (needs strands) ---


class _FakeModelCallEvt:
    """Stand-in for Strands BeforeModelCallEvent (only `cancel` is written)."""

    def __init__(self):
        self.cancel = False


def _hook(user_id, quota, monkeypatch, *, day=0, month=0, minute=0, raise_read=False):
    """Build a QuotaHook with the counter reads stubbed (no AWS)."""
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    pytest.importorskip("strands")
    from tokenomics import quota_counter as qc
    from tokenomics.governance_outcome import GovernanceOutcome
    from tokenomics.hooks.quota import QuotaHook

    if raise_read:

        def _boom(_u):
            raise qc.CounterUnavailable("boom")

        monkeypatch.setattr(qc, "read_day", _boom)
        monkeypatch.setattr(qc, "read_month", _boom)
        monkeypatch.setattr(qc, "read_minute", _boom)
    else:
        monkeypatch.setattr(qc, "read_day", lambda _u: day)
        monkeypatch.setattr(qc, "read_month", lambda _u: month)
        monkeypatch.setattr(qc, "read_minute", lambda _u: minute)

    oc = GovernanceOutcome()
    return QuotaHook(user_id, quota, oc), oc


def test_quota_hook_blocks_over_daily(monkeypatch):
    from shared.usage_plans_schema import UserQuota

    h, oc = _hook("bob", UserQuota(daily_max_tokens_per_user=100), monkeypatch, day=150)
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel  # over daily -> turn cancelled
    assert oc.reason == "quota" and oc.scope == "user"
    assert oc.config == "daily_max_tokens_per_user" and oc.stopped


def test_quota_hook_blocks_over_monthly(monkeypatch):
    from shared.usage_plans_schema import UserQuota

    h, oc = _hook("bob", UserQuota(monthly_max_tokens_per_user=1000), monkeypatch, month=1200)
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel and oc.reason == "quota"
    assert oc.config == "monthly_max_tokens_per_user"


def test_quota_hook_throttles_over_tpm(monkeypatch):
    from shared.usage_plans_schema import UserQuota

    h, oc = _hook("bob", UserQuota(max_tokens_per_minute_per_user=40000), monkeypatch, minute=50000)
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel and oc.reason == "throttle" and oc.scope == "user"
    assert oc.config == "max_tokens_per_minute_per_user"


def test_quota_hook_allows_under_cap(monkeypatch):
    from shared.usage_plans_schema import UserQuota

    h, oc = _hook("bob", UserQuota(daily_max_tokens_per_user=100), monkeypatch, day=10)
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel is False and not oc.acted()


def test_quota_hook_fail_closed_on_counter_error(monkeypatch):
    from shared.usage_plans_schema import UserQuota

    h, oc = _hook("bob", UserQuota(daily_max_tokens_per_user=100), monkeypatch, raise_read=True)
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel  # fail-closed: a read error BLOCKS (money limit)
    assert oc.reason == "quota" and oc.stopped


def test_quota_hook_noop_when_no_caps_benchmark(monkeypatch):
    """Empty config (what a benchmark pins) => no caps => hook never fires or reads."""
    from shared.usage_plans_schema import GovernanceConfig

    # a read stub that would fail the test if the hook ever touched the counter
    def _should_not_read(_u):
        raise AssertionError("quota hook must not read counters when there are no caps")

    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    pytest.importorskip("strands")
    from tokenomics import quota_counter as qc
    from tokenomics.governance_outcome import GovernanceOutcome
    from tokenomics.hooks.quota import QuotaHook

    monkeypatch.setattr(qc, "read_day", _should_not_read)
    monkeypatch.setattr(qc, "read_month", _should_not_read)
    monkeypatch.setattr(qc, "read_minute", _should_not_read)

    q = GovernanceConfig.empty().quota_for("bob")  # all-zero quota
    oc = GovernanceOutcome()
    h = QuotaHook("bob", q, oc)
    assert h._has_caps() is False
    e = _FakeModelCallEvt()
    h._check(e)
    assert e.cancel is False and not oc.acted()


# ---- metering emit routing: reason -> DetailType (fire-and-forget) ----------


def test_metering_emit_routes_by_reason(monkeypatch):
    for k in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
        monkeypatch.setenv(k, "x")
    from tokenomics import metering

    sent: list = []
    monkeypatch.setattr(metering, "_emit", lambda dt, detail: sent.append((dt, detail)))

    metering.emit_governance_event(
        user_id="bob", session_id="s1", governance={"reason": "quota", "config": "daily_max_tokens_per_user"}
    )
    metering.emit_governance_event(
        user_id="bob",
        session_id="s1",
        governance={"reason": "throttle", "config": "max_tokens_per_minute_per_user"},
    )
    metering.emit_governance_event(
        user_id="bob", session_id="s1", governance={"reason": "limit_turns", "config": "max_iterations"}
    )
    assert [dt for dt, _ in sent] == ["QuotaBlock", "ThrottleEvent", "LimitViolation"]
