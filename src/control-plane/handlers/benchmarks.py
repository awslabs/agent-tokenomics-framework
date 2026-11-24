"""/benchmarks handler -- start, poll, and list benchmarks.

A benchmark is ONE record (one DynamoDB item, keyed by the existing `run_id`
attribute used as the benchmark id). It holds BOTH runs nested inside it:

  * run_a  (baseline)  -- features = run_a.flags
  * run_b  (candidate) -- features = run_b.flags

plus a two-phase `progress` object (drives the side-by-side bars), and an
embedded `comparison` (so the A/B delta needs no separate call). The N x M loop
runs on a SEPARATE async-invoked worker Lambda (InvocationType="Event"): on AWS
the request Lambda freezes the instant the 202 returns, so a background thread
would never run. start_benchmark creates the record (status RUNNING) and kicks
off the worker, which runs Run A fully then Run B, writing progress/results back
into the single record.

Per-run breakdown is split into:
  * metrics       -- the core stats (tokens in/out, cost, model calls, cost/sess)
  * feature_stats -- an ORDERED, OPEN list of {key,value} metadata that grows per
                     enabled optimization strategy (e.g. prompt_cache adds cache
                     hit rate + cache-read tokens). New strategies append here with
                     no schema change.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

from handlers import load_generator
from lib import metrics, store
from shared.flags_schema import ALL_FEATURES, FlagConfig


@lru_cache(maxsize=1)
def _lambda_client():
    import config

    return boto3.client("lambda", region_name=config.AWS_REGION)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _enabled_features(flag_cfg: FlagConfig) -> list[str]:
    """The enabled flag ids for a run (badges + feature-specific stats)."""
    return [name for name in ALL_FEATURES if getattr(flag_cfg, name).enabled]


def _clamp_1_5(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(5, n))


def _compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _feature_stats(features: list[str], m: dict[str, Any]) -> list[dict[str, str]]:
    """Build the ORDERED, OPEN feature-metadata list for a run.

    Core stats live in `metrics`; this is the per-strategy extra metadata shown
    under "Breakdown". Each enabled strategy contributes its own {key,value}
    rows. Add new strategies here (or have them emit their own) -- the UI renders
    whatever is in this list, so no frontend change is needed per strategy.
    """
    stats: list[dict[str, str]] = []
    if "prompt_cache" in features:
        stats.append({"key": "Cache hit rate", "value": f"{m.get('cache_hit_ratio', 0.0) * 100:.1f}%"})
        stats.append({"key": "Cache-read tokens", "value": _compact(int(m.get("cache_read_tokens", 0)))})
    if "prompt_cache" in features:
        stats.append({"key": "Cache-read cost", "value": f"${m.get('cache_read_cost_usd', 0.0):.4f}"})
        stats.append({"key": "Cache-write cost", "value": f"${m.get('cache_write_cost_usd', 0.0):.4f}"})
    if "context_management" in features:
        stats.append({"key": "Tool blocks masked", "value": str(int(m.get("tool_blocks_masked", 0)))})
        stats.append({"key": "Messages trimmed", "value": str(int(m.get("messages_removed", 0)))})
        stats.append({"key": "Summarizations", "value": str(int(m.get("summarizations", 0)))})
        stats.append({"key": "Summarizer cost", "value": f"${m.get('summarizer_cost_usd', 0.0):.4f}"})
    if "model_routing" in features:
        capable = int(m.get("routing_capable_calls", 0))
        cheap = int(m.get("routing_cheap_calls", 0))
        tot = capable + cheap
        if tot:
            stats.append(
                {
                    "key": "Routing distribution",
                    "value": f"Capable {capable * 100 // tot}% · Cheap {cheap * 100 // tot}%",
                }
            )
        stats.append(
            {
                "key": "Orchestrator steps routed to cheap model",
                "value": str(int(m.get("routing_orch_cheap_calls", 0))),
            }
        )
        stats.append(
            {
                "key": "Specialist steps escalated to capable model",
                "value": str(int(m.get("routing_escalated_calls", 0))),
            }
        )
        stats.append({"key": "Judge cost", "value": f"${m.get('routing_judge_cost_usd', 0.0):.4f}"})
    # Future strategies append their stats here (e.g. tool cache).
    return stats


# ---- the single benchmark record -------------------------------------------


def _run_block(flag_cfg: FlagConfig, kind: str) -> dict[str, Any]:
    """Initial per-run block (nested inside the benchmark record)."""
    return {
        "kind": kind,  # "baseline" | "candidate"
        "features": _enabled_features(flag_cfg),
        "applied_flags": flag_cfg.to_dict(),
        "flag_config": flag_cfg.flag_signature(),
        "status": "RUNNING",
        "metrics": {},
        "feature_stats": [],
        "per_agent": {},
        "conversations": [],
        "error": None,
    }


def _make_benchmark(
    benchmark_id: str,
    *,
    name: str,
    run_a_cfg: FlagConfig,
    run_b_cfg: FlagConfig,
    sessions: int,
    turns: int,
) -> dict[str, Any]:
    """Build the initial RUNNING benchmark record (one item, both runs nested)."""
    total = sessions * turns
    return {
        "run_id": benchmark_id,  # table PK (reuse the existing key attribute)
        "name": name,
        "status": "RUNNING",
        "scenario": {
            "sessions": sessions,
            "turns": turns,
            "total_turns": total,
        },
        "progress": {
            "phase": "a",  # "a" | "b" | "done"
            "a": {"done": 0, "total": total},
            "b": {"done": 0, "total": total},
        },
        "run_a": _run_block(run_a_cfg, "baseline"),
        "run_b": _run_block(run_b_cfg, "candidate"),
        "comparison": {},
        "created_at": _now_iso(),
        "completed_at": None,
        "error": None,
    }


def _finalize_run_block(
    block: dict[str, Any], conversations: list[dict[str, Any]], realized_sessions: int, realized_turns: int
) -> None:
    """Fill a run block's metrics/feature_stats/per_agent from its conversations."""
    m = metrics.aggregate(conversations)
    block["metrics"] = m
    block["feature_stats"] = _feature_stats(block.get("features", []), m)
    block["per_agent"] = metrics.per_agent(conversations)
    block["per_turn"] = metrics.per_turn(conversations)
    block["conversations"] = conversations
    block["realized_sessions"] = realized_sessions
    block["realized_turns"] = realized_turns
    block["status"] = "COMPLETED"


def run_benchmark_sync(
    benchmark_id: str,
    *,
    sessions: int,
    turns: int,
    run_a_flags: dict[str, Any],
    run_b_flags: dict[str, Any],
) -> None:
    """Run baseline (A) fully, then candidate (B); write progress + results into
    the single benchmark record. Used by the worker Lambda.
    """

    def _phase(which: str, flags: dict[str, Any]) -> None:
        # Per-turn progress callback updates this run's bar inside the record.
        def on_progress(done: int, total: int) -> None:
            bm = store.get_run(benchmark_id) or {}
            prog = bm.get("progress") or {}
            prog.setdefault(which, {})
            prog[which] = {"done": done, "total": total}
            prog["phase"] = which
            bm["progress"] = prog
            store.upsert_run(bm)

        conversations, rs, rt = load_generator.run_scenarios(
            f"{benchmark_id}-{which}",  # unique session namespace per run
            flags=flags,
            sessions=sessions,
            turns=turns,
            on_progress=on_progress,
        )
        bm = store.get_run(benchmark_id) or {}
        block = bm["run_a"] if which == "a" else bm["run_b"]
        _finalize_run_block(block, conversations, rs, rt)
        bm["progress"][which] = {"done": rs * rt, "total": rs * rt}
        store.upsert_run(bm)

    try:
        _phase("a", run_a_flags)
        _phase("b", run_b_flags)

        bm = store.get_run(benchmark_id) or {}
        bm["comparison"] = _comparison(bm["run_a"], bm["run_b"])
        bm["progress"]["phase"] = "done"
        bm["status"] = "COMPLETED"
        bm["completed_at"] = _now_iso()
        store.upsert_run(bm)
    except Exception as exc:  # never leave a benchmark stuck RUNNING
        bm = store.get_run(benchmark_id) or {}
        bm["status"] = "FAILED"
        bm["error"] = f"{type(exc).__name__}: {exc}"
        bm["completed_at"] = _now_iso()
        store.upsert_run(bm)


def _comparison(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """Embedded A/B delta: gain = (cost_a - cost_b) / cost_a."""
    a = run_a.get("metrics") or {}
    b = run_b.get("metrics") or {}
    cost_a = float(a.get("cost_usd", 0.0))
    cost_b = float(b.get("cost_usd", 0.0))
    gain_pct = round((cost_a - cost_b) / cost_a, 4) if cost_a else 0.0
    return {"cost_a": round(cost_a, 6), "cost_b": round(cost_b, 6), "gain_pct": gain_pct}


def start_benchmark(body: dict[str, Any]) -> dict[str, Any]:
    """POST /benchmarks -> create ONE benchmark record + async-invoke the worker.

    Body:
      {
        "name"?: str,
        "scenario": { "sessions": 1..5, "turns": 1..5 },
        "run_a": { "flags": {...} },   # baseline
        "run_b": { "flags": {...} }    # candidate
      }
    Returns the RUNNING benchmark record immediately (202); the UI polls one id.
    """
    scenario = body.get("scenario") or {}
    sessions = _clamp_1_5(scenario.get("sessions"), 3)
    turns = _clamp_1_5(scenario.get("turns"), 3)

    run_a_cfg = FlagConfig.from_dict((body.get("run_a") or {}).get("flags") or {})
    run_b_cfg = FlagConfig.from_dict((body.get("run_b") or {}).get("flags") or {})

    name = body.get("name") or f"benchmark-{_now_iso()}"

    benchmark_id = f"bm-{uuid.uuid4().hex[:10]}"

    bm = _make_benchmark(
        benchmark_id,
        name=name,
        run_a_cfg=run_a_cfg,
        run_b_cfg=run_b_cfg,
        sessions=sessions,
        turns=turns,
    )
    store.upsert_run(bm)

    import config

    payload = {
        "benchmark_id": benchmark_id,
        "sessions": sessions,
        "turns": turns,
        "run_a_flags": run_a_cfg.to_dict(),
        "run_b_flags": run_b_cfg.to_dict(),
    }
    try:
        _lambda_client().invoke(
            FunctionName=config.BENCHMARK_WORKER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:  # never leave the benchmark stuck RUNNING
        bm = store.get_run(benchmark_id) or {}
        bm["status"] = "FAILED"
        bm["error"] = f"failed to start benchmark worker: {type(exc).__name__}: {exc}"
        bm["completed_at"] = _now_iso()
        store.upsert_run(bm)
        return store.get_run(benchmark_id) or bm

    return bm


def get_benchmark(benchmark_id: str) -> dict[str, Any] | None:
    """GET /benchmarks/{id} -> the full benchmark record (both runs + comparison)."""
    return store.get_run(benchmark_id)


def list_benchmarks() -> list[dict[str, Any]]:
    """GET /benchmarks -> one summary row per benchmark, newest first."""
    out = []
    for bm in store.list_runs():
        a = bm.get("run_a") or {}
        b = bm.get("run_b") or {}
        out.append(
            {
                "run_id": bm.get("run_id"),
                "name": bm.get("name"),
                "status": bm.get("status"),
                "scenario": bm.get("scenario"),
                "progress": bm.get("progress"),
                "features_a": a.get("features"),
                "features_b": b.get("features"),
                "cost_a": (a.get("metrics") or {}).get("cost_usd"),
                "cost_b": (b.get("metrics") or {}).get("cost_usd"),
                "comparison": bm.get("comparison"),
                "created_at": bm.get("created_at"),
                "completed_at": bm.get("completed_at"),
            }
        )
    return out
