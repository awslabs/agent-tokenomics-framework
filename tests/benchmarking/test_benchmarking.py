"""LIVE benchmarking test — runs a real multi-turn A/B benchmark end to end.

This is the same thing the Benchmarks page does when you click "Run": it starts
ONE benchmark (two linked runs on identical inputs) against the DEPLOYED stack,
the agent actually answers every turn on AgentCore, and we poll to COMPLETED and
assert + print the A-vs-B comparison.

Scenario: 2 sessions x 3 turns (= 6 turns per run, 12 agent turns total).
  * Run A (baseline)  : memory ON only.
  * Run B (candidate) : memory ON + prompt caching ON.

Prompt caching should make Run B cheaper on the repeated multi-turn prefix, so
we expect a positive gain (A - B)/A. We assert the mechanics (both runs COMPLETE,
comparison present, caching produced cache reads) rather than a hard savings % —
LLM/runtime variability makes an exact number brittle.

COST: sends 12 short real agent turns (small Bedrock spend). Benchmarks run
ungoverned + tagged `source=benchmark`, so this never trips a quota or pollutes
live analytics.

Run:
    bash tests/benchmarking/test-benchmarking.sh
    src/app-plane/.venv/bin/python -m pytest tests/benchmarking -m live -v -s
"""

from __future__ import annotations

import time

import boto3
import pytest
from conftest import REGION

pytestmark = pytest.mark.live

SESSIONS = 2
TURNS = 3
TOTAL_TURNS = SESSIONS * TURNS

# A benchmark = Run A then Run B, each 6 turns against the live agent. Poll
# generously: 12 real agent turns can take a couple of minutes end to end.
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 420


def _run_a_b_benchmark(bench) -> dict:
    """Start a 2x3 A/B benchmark and poll the single record to a terminal state."""
    started = bench.start(
        {
            "name": f"live-test-{int(time.time())}",
            "scenario": {"sessions": SESSIONS, "turns": TURNS},
            "run_a": {"flags": {"memory": {"enabled": True}}},
            "run_b": {"flags": {"memory": {"enabled": True}, "prompt_cache": {"enabled": True}}},
        }
    )
    benchmark_id = started["run_id"]
    assert started["status"] == "RUNNING", started
    print(f"\n[bench] started {benchmark_id} ({SESSIONS}x{TURNS}); polling…", flush=True)

    start_ts = time.time()
    deadline = start_ts + POLL_TIMEOUT_S
    bm = started
    while time.time() < deadline:
        bm = bench.get(benchmark_id)
        prog = bm.get("progress") or {}
        phase = prog.get("phase", "?")
        a_done = (prog.get("a") or {}).get("done", 0)
        b_done = (prog.get("b") or {}).get("done", 0)
        elapsed = int(time.time() - start_ts)
        # Heartbeat: which phase, turns done on each side, elapsed seconds.
        print(
            f"[bench] {elapsed:3d}s  status={bm['status']:<9} phase={phase}  "
            f"A {a_done}/{TOTAL_TURNS}  B {b_done}/{TOTAL_TURNS}",
            flush=True,
        )
        if bm["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(POLL_INTERVAL_S)

    assert bm["status"] == "COMPLETED", f"benchmark did not complete: {bm.get('status')} / {bm.get('error')}"
    return bm


def _print_comparison(bm: dict) -> None:
    """Print the A-vs-B summary the Benchmarks page shows (via `-s`)."""
    a, b = bm["run_a"], bm["run_b"]
    cmp = bm.get("comparison") or {}
    ma, mb = a.get("metrics") or {}, b.get("metrics") or {}
    gain = cmp.get("gain_pct", 0.0)
    print("\n" + "=" * 68)
    print(f"Benchmark Results — {bm['name']}  ({SESSIONS} sessions x {TURNS} turns)")
    print("=" * 68)
    print(f"{'':20} {'Run A (baseline)':>20} {'Run B (candidate)':>20}")
    print(f"{'features':20} {','.join(a['features']):>20} {','.join(b['features']):>20}")
    print(f"{'cost ($)':20} {ma.get('cost_usd', 0):>20.6f} {mb.get('cost_usd', 0):>20.6f}")
    print(f"{'input tokens':20} {ma.get('input_tokens', 0):>20} {mb.get('input_tokens', 0):>20}")
    print(
        f"{'cache read tokens':20} {ma.get('cache_read_tokens', 0):>20} {mb.get('cache_read_tokens', 0):>20}"
    )
    print(
        f"{'cache hit ratio':20} {ma.get('cache_hit_ratio', 0):>20.3f} {mb.get('cache_hit_ratio', 0):>20.3f}"
    )
    print(f"{'model calls':20} {ma.get('model_calls', 0):>20} {mb.get('model_calls', 0):>20}")
    print("-" * 68)
    print(f"GAIN (A-B)/A : {gain * 100:.1f}%  (positive = Run B cheaper)")
    print("=" * 68)


def test_live_ab_benchmark_runs_and_compares(bench):
    """Run a real 2x3 A/B benchmark and assert the end-to-end comparison."""
    bm = _run_a_b_benchmark(bench)
    _print_comparison(bm)

    a, b = bm["run_a"], bm["run_b"]

    # Both runs completed with the right pinned feature sets.
    assert a["status"] == "COMPLETED" and b["status"] == "COMPLETED"
    assert set(a["features"]) == {"memory"}
    assert set(b["features"]) == {"memory", "prompt_cache"}

    # Each run actually ran the full 2x3 matrix (6 conversations) against the agent.
    assert len(a["conversations"]) == TOTAL_TURNS
    assert len(b["conversations"]) == TOTAL_TURNS
    assert all(not c.get("error") and c.get("reply") for c in a["conversations"]), (
        "a turn errored / had no reply"
    )
    assert all(not c.get("error") and c.get("reply") for c in b["conversations"]), (
        "a turn errored / had no reply"
    )

    # Real token/cost metrics aggregated for both sides.
    ma, mb = a["metrics"], b["metrics"]
    assert ma["cost_usd"] > 0 and mb["cost_usd"] > 0
    assert ma["model_calls"] > 0 and mb["model_calls"] > 0

    # Prompt caching on B produced cache reads; baseline A has none.
    assert ma["cache_read_tokens"] == 0, "baseline (no prompt_cache) must not read cache"
    assert mb["cache_read_tokens"] > 0, "candidate (prompt_cache on) should read cache on repeat turns"

    # Embedded comparison is present and self-consistent with the two runs.
    cmp = bm["comparison"]
    assert cmp["cost_a"] == pytest.approx(ma["cost_usd"], rel=1e-6)
    assert cmp["cost_b"] == pytest.approx(mb["cost_usd"], rel=1e-6)
    expected_gain = (ma["cost_usd"] - mb["cost_usd"]) / ma["cost_usd"]
    assert cmp["gain_pct"] == pytest.approx(expected_gain, rel=1e-3)

    # B's dynamic feature_stats include the prompt-cache metadata rows.
    b_stat_keys = {s["key"] for s in (b.get("feature_stats") or [])}
    assert "Cache hit rate" in b_stat_keys


def test_benchmark_turns_tagged_source_benchmark(bench, stack):
    """Benchmark spend lands in BENCH#RUN# rows (source=benchmark), never live rows.

    Confirms the isolation guarantee: a benchmark never pollutes the live
    USER#/SESSION#/AGENT# roll-ups the Governance/Analytics pages read.
    """
    bm = _run_a_b_benchmark(bench)
    _print_comparison(bm)

    ddb = boto3.client("dynamodb", region_name=REGION)
    # Scan the metrics table for rows referencing this benchmark id.
    resp = ddb.scan(
        TableName=stack.metrics_table,
        FilterExpression="contains(pk, :bid)",
        ExpressionAttributeValues={":bid": {"S": bm["run_id"]}},
        ProjectionExpression="pk, #s",
        ExpressionAttributeNames={"#s": "source"},
    )
    rows = resp.get("Items", [])
    assert rows, "expected BENCH# metric rows for the benchmark"
    for row in rows:
        pk = row["pk"]["S"]
        assert pk.startswith("BENCH#"), f"benchmark spend leaked into a live row: {pk}"
        assert row.get("source", {}).get("S") == "benchmark", f"row not tagged source=benchmark: {pk}"
