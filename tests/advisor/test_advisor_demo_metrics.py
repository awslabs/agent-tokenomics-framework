"""The Advisor demo-metrics seed produces plausible rows the Advisor can analyse (no AWS).

Runs the seed's synthetic UsageRecorded events through the REAL metering consumer
into an in-memory store, then asks the Advisor Service tools about them.

    src/app-plane/.venv/bin/python -m pytest tests/advisor/test_advisor_demo_metrics.py -q
"""

from __future__ import annotations

import importlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def seeded(monkeypatch, cp_import):
    store = cp_import("lib.store")
    rows: dict[str, dict] = defaultdict(dict)

    def add_usage(pk, tokens, cost, extra=None, turns=0):
        r = rows[pk]
        r["pk"] = pk
        r["tokens"] = r.get("tokens", 0) + tokens
        r["cost_usd"] = r.get("cost_usd", 0.0) + cost
        if turns:
            r["turns"] = r.get("turns", 0) + turns
        extra = dict(extra or {})
        for a, v in (extra.pop("split", None) or {}).items():
            r[a] = r.get(a, 0) + v
        if "ts" in extra:
            r["last_ts"] = extra.pop("ts")
        r.update({k: v for k, v in extra.items() if v})

    def append_turn(sid, entry):
        r = rows[f"TURNS#{sid}"]
        r["pk"] = f"TURNS#{sid}"
        r.setdefault("turn_log", []).append(entry)
        return True

    monkeypatch.setattr(store, "add_metric_usage", add_usage)
    monkeypatch.setattr(store, "append_turn", append_turn)
    mc = importlib.import_module("metering_consumer")
    demo = importlib.import_module("advisor_demo_metrics")
    events = demo.build_events(30, 42)
    assert all(mc.handler(e, None)["ok"] for e in events)
    g = importlib.import_module("handlers.governance")
    svc = importlib.import_module("advisor.service")
    live = [v for k, v in rows.items() if not k.startswith("TURNS#")]
    monkeypatch.setattr(g, "_email_for_sub", lambda sub: sub)
    monkeypatch.setattr(g, "_sub_for_email", lambda e: None if e in ("", "all") else e)
    monkeypatch.setattr(g.store, "list_metrics", lambda prefix=None: list(live))
    monkeypatch.setattr(g.store, "list_events", lambda limit=100: [])
    monkeypatch.setattr(g.store, "read_governance_config", lambda: g.store.default_governance_config())
    ctx = svc.AdvisorContext(live, {}, {}, get_row=lambda pk: rows.get(pk))
    return events, rows, svc, ctx


def test_rows_are_plausible_and_demo_marked(seeded):
    events, rows, _, _ = seeded
    assert 400 < len(events) < 3000
    assert all("-demo" in pk for pk in rows)
    turn_costs = [e["detail"]["usage"]["cost_usd"] for e in events]
    assert 0.008 < sorted(turn_costs)[len(turn_costs) // 2] < 0.05  # ~1-2 cents a turn, like live traffic
    assert any(pk.startswith("MODEL#global.anthropic.claude-sonnet") for pk in rows)
    assert any(pk.startswith("TURNS#") for pk in rows)
    agent = next(r for pk, r in rows.items() if pk.startswith("AGENT#orchestrator#"))
    assert agent["model_id"].startswith("global.anthropic.claude-sonnet") and "input_tokens" in agent


def test_advisor_finds_the_story(seeded):
    _, _, svc, ctx = seeded
    out = svc.get_outliers(ctx, "7d")
    assert "max-demo" in [u["who"] for u in out["labels"]["users"]]
    cache = svc.get_cache_health(ctx, "30d")
    drop = cache["labels"]["first_drop_day"]
    today = datetime.now(UTC).date()
    assert drop and today - timedelta(days=6) <= datetime.strptime(drop, "%Y-%m-%d").date() <= today
    spike_day = (today - timedelta(days=2)).strftime("%Y%m%d")
    curve = svc.get_turn_curve(ctx, f"s-max-demo-{spike_day}-1")
    assert curve["available"] and curve["labels"]["spike_turn"] == 6
    assert f"s-max-demo-{spike_day}-1" in [
        x["session_id"] for x in out["labels"]["sessions"]
    ]  # session outlier
    assert len(out["labels"]["agents"]) <= 1  # agents compared with their own typical day
    cmp_ = svc.get_period_comparison(ctx, "7d")
    assert cmp_["figures"]["total.previous"]["value"] > 0
    summary = svc.get_cost_summary(ctx, "30d")
    assert summary["labels"]["models"] and summary["figures"]["users.count"]["value"] == 8
