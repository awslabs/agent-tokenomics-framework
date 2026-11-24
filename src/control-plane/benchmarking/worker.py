"""Async benchmark worker Lambda.

Invoked with InvocationType="Event" by the control-plane API Lambda
(handlers/benchmarks.start_benchmark). AWS runs this to completion in its own
execution environment, so the N x M conversation loop is NOT subject to the
request-Lambda freeze. It runs Run A (baseline) fully, then Run B (candidate),
reusing the existing run logic (handlers.benchmarks.run_benchmark_sync ->
load_generator.run_scenarios). Per-phase, per-turn progress + COMPLETED/FAILED
are written into the single benchmark record.
"""

from __future__ import annotations

# --- import bootstrap: make `handlers`, `lib`, `config`, `shared` importable ---
# This module lives in src/control-plane/benchmarking/, so the control-plane
# root (one level up) must be on sys.path for the handler/lib/config imports.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CP_ROOT = os.path.dirname(_HERE)  # src/control-plane
for p in (_CP_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
# ------------------------------------------------------------------------------

from typing import Any

from handlers import benchmarks as benchmarks_handler
from lib import store
from shared.logging_setup import configure_logging

configure_logging()  # Lambda root handler: set our level so INFO logs reach CloudWatch


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run both runs of a benchmark. `event` carries the benchmark id + config.

    Async Lambda ("Event") deliveries can arrive more than once and failed async
    executions are retried, so this guards against running the same benchmark
    twice: it atomically claims the record before entering the loop. The first
    delivery wins; every later delivery (a concurrent duplicate OR an AWS async
    retry) loses the race and returns early WITHOUT re-running (no double
    AgentCore cost, no BENCH#RUN# overcount, no replay into dirty memory). A
    worker killed mid-run leaves its benchmark RUNNING by design (an idempotent
    re-run / reaper is out of scope); the kickoff-failure path in start_benchmark
    still marks a never-started benchmark FAILED.
    """
    benchmark_id = event["benchmark_id"]
    sessions = int(event["sessions"])
    turns = int(event["turns"])
    run_a_flags = event.get("run_a_flags") or {}
    run_b_flags = event.get("run_b_flags") or {}

    if not store.claim_run_for_worker(benchmark_id):
        return {"benchmark_id": benchmark_id, "status": "skipped"}

    benchmarks_handler.run_benchmark_sync(
        benchmark_id,
        sessions=sessions,
        turns=turns,
        run_a_flags=run_a_flags,
        run_b_flags=run_b_flags,
    )
    return {"benchmark_id": benchmark_id, "status": "done"}
