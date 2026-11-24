"""Unit tests for the Advisor's metrics enrichment (R9a/R9b/R10), no AWS.

Run from the repo root:
    src/app-plane/.venv/bin/python -m pytest tests/advisor/test_advisor_metering.py -q

Covers the additive writes in `metering_consumer` and `lib.store`:
  - R9b split attributes + R9a `model_id` on the existing live roll-ups
  - new MODEL#<model>#USER#<sub>#DAY#<d> rows and the TURNS#<session_id> ledger
  - the existing USER#/AGENT#/SESSION# writes keep their keys and values
  - benchmark traffic writes none of the new rows
  - `governance.analytics()` output is unchanged when the new rows are present
  - `list_metrics` pagination
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

ORCH = "global.anthropic.claude-sonnet-5-5"
HAIKU = "us.anthropic.claude-haiku-5-5"


@pytest.fixture(autouse=True)
def _env(monkeypatch, cp_import):
    # cp_import sets the table env vars and sys.path for every test here
    monkeypatch.delenv("APP_USER_POOL_ID", raising=False)


def _agent(model_id, it, ot, cr=0, cw=0, cost=0.0, calls=1):
    return {
        "input_tokens": it,
        "output_tokens": ot,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "total_input_tokens": it + cr + cw,
        "cost_usd": cost,
        "model_id": model_id,
        "model_calls": calls,
    }


def _live_detail():
    orch = _agent(ORCH, 1000, 200, cr=500, cw=100, cost=0.01)
    order = _agent(HAIKU, 300, 50, cost=0.002)
    return {
        "user_id": "bob",
        "session_id": "s1",
        "ts": "2026-09-25T10:00:00+00:00",
        "usage": {
            "input_tokens": 1300,
            "output_tokens": 250,
            "cache_read_tokens": 500,
            "cache_write_tokens": 100,
            "total_input_tokens": 1900,
            "cost_usd": 0.012,
            "by_agent": {"orchestrator": orch, "order": order},
        },
        "governance": {"reason": "tool_cap", "stopped": False, "limited": True},
        "model_ids": {"orchestrator": ORCH, "knowledge": HAIKU, "order": HAIKU},
    }


def _consumer_with_stub(monkeypatch, *, append_turn=None):
    """Reload metering_consumer against a recording fake `lib.store`."""
    adds: list = []
    turns: list = []
    store = types.ModuleType("lib.store")
    store.SPLIT_ATTRS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    store.add_metric_usage = lambda pk, t, c, extra=None, turns=0: adds.append(
        {"pk": pk, "tokens": t, "cost": c, "extra": dict(extra or {}), "turns": turns}
    )
    store.add_metric_count = lambda pk, n=1, extra=None: None
    store.append_event = lambda e: None

    def _append(session_id, entry):
        turns.append({"session_id": session_id, "entry": entry})
        return True

    store.append_turn = append_turn or _append
    libpkg = types.ModuleType("lib")
    libpkg.store = store
    monkeypatch.setitem(sys.modules, "lib", libpkg)
    monkeypatch.setitem(sys.modules, "lib.store", store)
    mc = importlib.import_module("metering_consumer")
    importlib.reload(mc)
    return mc, adds, turns


# ---- consumer --------------------------------------------------------------


def test_existing_live_rollups_keep_keys_and_values(monkeypatch):
    mc, adds, _ = _consumer_with_stub(monkeypatch)
    assert mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)["ok"]
    by_pk = {a["pk"]: a for a in adds}
    # Same pks, totals and turn counters as before the enrichment.
    assert by_pk["SESSION#s1"]["tokens"] == 2150 and by_pk["SESSION#s1"]["cost"] == 0.012
    assert by_pk["SESSION#s1"]["turns"] == 1
    assert by_pk["USER#bob#DAY#2026-09-25"]["tokens"] == 2150
    assert by_pk["USER#bob#DAY#2026-09-25"]["turns"] == 1
    orch = by_pk["AGENT#orchestrator#USER#bob#DAY#2026-09-25"]
    assert orch["tokens"] == 1800 and orch["cost"] == 0.01 and orch["turns"] == 0
    for row in (by_pk["SESSION#s1"], by_pk["USER#bob#DAY#2026-09-25"], orch):
        assert row["extra"]["source"] == "live" and row["extra"]["user_id"] == "bob"


def test_split_and_model_id_on_live_rollups(monkeypatch):
    mc, adds, _ = _consumer_with_stub(monkeypatch)
    mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)
    by_pk = {a["pk"]: a for a in adds}
    assert by_pk["SESSION#s1"]["extra"]["split"] == {
        "input_tokens": 1300,
        "output_tokens": 250,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
    }
    orch = by_pk["AGENT#orchestrator#USER#bob#DAY#2026-09-25"]["extra"]
    assert orch["model_id"] == ORCH
    assert orch["split"] == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
    }
    assert by_pk["AGENT#order#USER#bob#DAY#2026-09-25"]["extra"]["model_id"] == HAIKU


def test_model_rollups_written_per_agent_model(monkeypatch):
    mc, adds, _ = _consumer_with_stub(monkeypatch)
    mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)
    model_rows = [a for a in adds if a["pk"].startswith("MODEL#")]
    assert {a["pk"] for a in model_rows} == {
        f"MODEL#{ORCH}#USER#bob#DAY#2026-09-25",
        f"MODEL#{HAIKU}#USER#bob#DAY#2026-09-25",
    }
    sonnet = next(a for a in model_rows if ORCH in a["pk"])
    assert sonnet["tokens"] == 1800 and sonnet["cost"] == 0.01
    assert sonnet["extra"] | {} == {
        "ts": "2026-09-25T10:00:00+00:00",
        "user_id": "bob",
        "day": "2026-09-25",
        "model_id": ORCH,
        "source": "live",
        "split": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 500,
            "cache_write_tokens": 100,
        },
    }


def test_model_id_with_hash_is_not_keyed(monkeypatch):
    mc, adds, _ = _consumer_with_stub(monkeypatch)
    d = _live_detail()
    d["usage"]["by_agent"]["order"]["model_id"] = "bad#id"
    mc.handler({"detail-type": "UsageRecorded", "detail": d}, None)
    assert not any("bad#id" in a["pk"] for a in adds)
    assert "model_id" not in {
        k for a in adds if a["pk"].startswith("AGENT#order") for k, v in a["extra"].items() if v
    }


def test_turn_ledger_entry(monkeypatch):
    mc, _, turns = _consumer_with_stub(monkeypatch)
    mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)
    assert len(turns) == 1
    t = turns[0]
    assert t["session_id"] == "s1"
    e = t["entry"]
    assert e["ts"] == "2026-09-25T10:00:00+00:00" and e["tokens"] == 2150 and e["cost_usd"] == 0.012
    assert e["input_tokens"] == 1300 and e["cache_read_tokens"] == 500
    assert e["agents"]["orchestrator"] == {"tokens": 1800, "cost_usd": 0.01, "model_calls": 1}
    assert e["governance"] == {"reason": "tool_cap", "stopped": False, "limited": True}


def test_benchmark_writes_no_advisor_rows(monkeypatch):
    mc, adds, turns = _consumer_with_stub(monkeypatch)
    d = _live_detail()
    d["run_id"] = "run7"
    assert mc.handler({"detail-type": "UsageRecorded", "detail": d}, None)["ok"]
    assert [a["pk"] for a in adds] == [
        "BENCH#RUN#run7",
        "BENCH#RUN#run7#AGENT#orchestrator",
        "BENCH#RUN#run7#AGENT#order",
    ]
    assert all("split" not in a["extra"] and "model_id" not in a["extra"] for a in adds)
    assert turns == []


def test_one_model_rollup_failure_keeps_the_others(monkeypatch):
    mc, adds, _ = _consumer_with_stub(monkeypatch)
    real_add = mc.store.add_metric_usage

    def flaky(pk, t, c, extra=None, turns=0):
        if pk.startswith(f"MODEL#{ORCH}"):
            raise RuntimeError("throttled")
        return real_add(pk, t, c, extra, turns)

    monkeypatch.setattr(mc.store, "add_metric_usage", flaky)
    assert mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)["ok"]
    assert f"MODEL#{HAIKU}#USER#bob#DAY#2026-09-25" in {a["pk"] for a in adds}


def test_turn_ledger_failure_never_drops_existing_rows(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("ddb down")

    mc, adds, _ = _consumer_with_stub(monkeypatch, append_turn=boom)
    assert mc.handler({"detail-type": "UsageRecorded", "detail": _live_detail()}, None)["ok"]
    pks = {a["pk"] for a in adds}
    assert {"SESSION#s1", "USER#bob#DAY#2026-09-25", "AGENT#orchestrator#USER#bob#DAY#2026-09-25"} <= pks


def test_unknown_session_skips_turn_ledger(monkeypatch):
    mc, _, turns = _consumer_with_stub(monkeypatch)
    d = _live_detail()
    d.pop("session_id")
    mc.handler({"detail-type": "UsageRecorded", "detail": d}, None)
    assert turns == []


# ---- lib.store -------------------------------------------------------------


class _FakeTable:
    def __init__(self, pages=None, fail_condition=False):
        self.updates: list = []
        self.scans: list = []
        self._pages = pages or [{"Items": []}]
        self._fail = fail_condition

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self._fail and "ConditionExpression" in kwargs:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        return self._pages[len(self.scans) - 1]


def _real_store(monkeypatch, cp_import, table):
    store = cp_import("lib.store")
    monkeypatch.setattr(store, "_metrics_table", lambda: table)
    return store


def test_add_metric_usage_split_and_model_id(monkeypatch, cp_import):
    t = _FakeTable()
    store = _real_store(monkeypatch, cp_import, t)
    store.add_metric_usage(
        "AGENT#o#USER#u#DAY#d",
        10,
        0.5,
        {
            "ts": "ts1",
            "user_id": "u",
            "model_id": ORCH,
            "split": {"input_tokens": 4, "output_tokens": 3, "cache_read_tokens": 2, "cache_write_tokens": 1},
        },
    )
    call = t.updates[0]
    expr = call["UpdateExpression"]
    assert expr.startswith("ADD tokens :t, cost_usd :c, #input_tokens :input_tokens")
    for attr in store.SPLIT_ATTRS:
        assert f"#{attr} :{attr}" in expr and call["ExpressionAttributeNames"][f"#{attr}"] == attr
    assert call["ExpressionAttributeValues"][":cache_read_tokens"] == 2
    assert "#model_id = :model_id" in expr and call["ExpressionAttributeValues"][":model_id"] == ORCH


def test_add_metric_usage_without_split_is_unchanged(monkeypatch, cp_import):
    t = _FakeTable()
    store = _real_store(monkeypatch, cp_import, t)
    store.add_metric_usage("SESSION#s", 5, 0.1, {"ts": "x", "user_id": "u", "source": "live"}, turns=1)
    call = t.updates[0]
    assert call["UpdateExpression"] == (
        "ADD tokens :t, cost_usd :c, #turns :turns SET last_ts = :ts, #user_id = :user_id, #source = :source"
    )


def test_append_turn_list_append_ttl_and_cap(monkeypatch, cp_import):
    t = _FakeTable()
    store = _real_store(monkeypatch, cp_import, t)
    monkeypatch.setattr(store.time, "time", lambda: 1_000_000)
    assert store.append_turn("s1", {"ts": "a", "cost_usd": 0.5})
    call = t.updates[0]
    assert call["Key"] == {"pk": "TURNS#s1"}
    assert call["UpdateExpression"] == (
        "SET #log = list_append(if_not_exists(#log, :empty), :entry), last_ts = :ts, #ttl = :ttl"
    )
    assert call["ExpressionAttributeNames"] == {"#log": "turn_log", "#ttl": "ttl"}
    assert call["ExpressionAttributeValues"][":ts"] == "a"
    assert call["ExpressionAttributeValues"][":ttl"] == 1_000_000 + 90 * 86400
    assert call["ExpressionAttributeValues"][":max"] == store.TURNS_MAX_ENTRIES == 200
    assert call["ConditionExpression"] == "attribute_not_exists(#log) OR size(#log) < :max"
    from decimal import Decimal

    assert call["ExpressionAttributeValues"][":entry"] == [{"ts": "a", "cost_usd": Decimal("0.5")}]

    full = _FakeTable(fail_condition=True)
    store = _real_store(monkeypatch, cp_import, full)
    assert store.append_turn("s1", {"ts": "b"}) is False


def test_list_metrics_follows_pagination(monkeypatch, cp_import):
    pages = [
        {"Items": [{"pk": "USER#a#DAY#d"}], "LastEvaluatedKey": {"pk": "USER#a#DAY#d"}},
        {"Items": [{"pk": "SESSION#s"}], "LastEvaluatedKey": {"pk": "SESSION#s"}},
        {"Items": [{"pk": "USER#b#DAY#d"}]},
    ]
    t = _FakeTable(pages=pages)
    store = _real_store(monkeypatch, cp_import, t)
    rows = store.list_metrics()
    assert [r["pk"] for r in rows] == ["USER#a#DAY#d", "SESSION#s", "USER#b#DAY#d"]
    ledger_filter = {
        "FilterExpression": "NOT begins_with(pk, :ledger)",
        "ExpressionAttributeValues": {":ledger": "TURNS#"},
    }
    assert t.scans[0] == ledger_filter
    assert t.scans[1] == {"ExclusiveStartKey": {"pk": "USER#a#DAY#d"}, **ledger_filter}
    t.scans.clear()
    assert [r["pk"] for r in store.list_metrics(prefix="USER#")] == ["USER#a#DAY#d", "USER#b#DAY#d"]


# ---- analytics is unaffected by the new rows --------------------------------


def test_analytics_identical_with_advisor_rows(monkeypatch, cp_import):
    g = cp_import("handlers.governance")
    day = g._window_days("7d")[0]
    base = [
        {
            "pk": "SESSION#s1",
            "tokens": 2150,
            "cost_usd": 0.012,
            "turns": 1,
            "user_id": "bob",
            "source": "live",
            "last_ts": f"{day}T10:00:00+00:00",
        },
        {
            "pk": f"USER#bob#DAY#{day}",
            "tokens": 2150,
            "cost_usd": 0.012,
            "turns": 1,
            "user_id": "bob",
            "day": day,
            "source": "live",
        },
        {
            "pk": f"AGENT#orchestrator#USER#bob#DAY#{day}",
            "tokens": 1800,
            "cost_usd": 0.01,
            "user_id": "bob",
            "day": day,
            "agent": "orchestrator",
            "source": "live",
        },
    ]
    advisor_rows = [
        {
            "pk": f"MODEL#{ORCH}#USER#bob#DAY#{day}",
            "tokens": 1800,
            "cost_usd": 0.01,
            "user_id": "bob",
            "day": day,
            "model_id": ORCH,
            "source": "live",
            "input_tokens": 1000,
        },
        {
            "pk": "TURNS#s1",
            "ttl": 1,
            "turn_log": [{"ts": f"{day}T10:00:00+00:00", "tokens": 2150, "cost_usd": 0.012}],
        },
    ]
    monkeypatch.setattr(g.store, "list_events", lambda limit=100: [])
    monkeypatch.setattr(g.store, "read_governance_config", lambda: g.store.default_governance_config())

    monkeypatch.setattr(g.store, "list_metrics", lambda prefix=None: list(base))
    before = g.analytics("all", "7d")
    monkeypatch.setattr(g.store, "list_metrics", lambda prefix=None: list(base) + advisor_rows)
    after = g.analytics("all", "7d")
    assert after == before
    assert after["kpis"]["total_sessions"] == 1 and after["kpis"]["total_turns"] == 1
