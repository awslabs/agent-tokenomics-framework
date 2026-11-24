"""Load generator -- replays the multi-turn scenario set against the agent.

This is the "input side" of a benchmark run. It replays a nested scenario corpus
(sessions -> ordered turns, from benchmarking/scenarios.json) at the agent, tagged
with the run's run_id, and returns each turn's usage so the run can be aggregated.

The parallelism unit is the SESSION, not the turn:
  * turns WITHIN a session run SEQUENTIALLY sharing ONE session_id, so short-term
    memory accumulates across the ordered turns of that conversation;
  * sessions run IN PARALLEL (distinct session_ids) via a thread pool, up to
    config.BENCHMARK_CONCURRENCY, so they are memory-isolated from one another.

The run's flag config is pinned PER REQUEST (passed in the AgentCore runtime
payload as `flags`), so a benchmark never races or clobbers the operator's live
flag table -- each turn runs under exactly the pinned config.

A benchmark also pins a NO-CAPS usage-plans config per request (`usage_plans`),
so the operator's live per-user quotas / TPM throttle never fire during a run and
the live UsagePlans table is left completely untouched. A run is isolated and
depends only on its benchmark settings.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import config
from lib import agent_client
from lib.agent_client import AgentError
from shared.usage_plans_schema import GovernanceConfig


def load_scenarios() -> dict[str, Any]:
    """Load the nested multi-turn corpus: { description, sessions }."""
    with open(config.SCENARIOS_FILE, encoding="utf-8") as f:
        return json.load(f)


def run_scenarios(
    run_id: str,
    *,
    flags: dict[str, Any] | None = None,
    sessions: int,
    turns: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Replay the first-N sessions x first-M turns against the agent.

    Subsets the authored corpus to the requested ``sessions`` (N) x ``turns`` (M),
    CLAMPING to the realized counts when the corpus is smaller on either axis.
    Sessions are dispatched to a thread pool (PARALLEL); the turns of a session
    run SEQUENTIALLY on one shared ``session_id = f"{run_id}-{s_index}"`` so
    short-term memory accumulates across them.

    Each request pins ``flags`` + a no-caps ``usage_plans`` so the whole run uses
    one known config and governance never fires. Returns
    ``(records, realized_sessions, realized_turns)`` where each per-turn record
    carries index / session / turn / user_id / message / reply / usage (usage
    includes ``by_agent``). A failed turn carries an ``error`` and empty usage so
    a single failure never sinks the whole run. Progress is reported in completed
    TURNS out of ``realized_sessions * realized_turns`` -- it fires once per turn
    (success OR error), so the bar advances smoothly even though sessions run in
    parallel. The per-turn ``done`` counter is guarded by a lock because multiple
    session threads increment it concurrently.
    """
    corpus = load_scenarios()
    all_sessions = corpus.get("sessions", []) or []

    # Subset first-N x first-M, clamping to what the corpus actually has.
    realized_sessions = min(sessions, len(all_sessions))
    selected = all_sessions[:realized_sessions]
    realized_turns = min(turns, min((len(s.get("turns", [])) for s in selected), default=0))

    # Progress is tracked at TURN granularity: total turns across all sessions.
    total = realized_sessions * realized_turns
    session_results: list[list[dict[str, Any]]] = [[] for _ in range(realized_sessions)]
    done = 0
    done_lock = threading.Lock()

    def _report_turn_done() -> None:
        """Increment the shared done counter (thread-safe) and report progress."""
        nonlocal done
        with done_lock:
            done += 1
            current = done
        if on_progress:
            on_progress(current, total)

    # Pin a no-caps usage-plans config for every request in this run so the
    # operator's live per-user quotas / TPM throttle never affect the benchmark
    # (and the live UsagePlans table is never touched -- the entrypoint applies
    # this as an in-memory override cleared after each turn).
    no_caps_plans = GovernanceConfig.empty().to_dict()

    def _run_session(s_index: int, session: dict[str, Any]) -> list[dict[str, Any]]:
        # Benchmark identities are synthetic (not real Cognito users). Send the
        # session's user_id + a minimal profile; order lookups return empty,
        # which is fine for cost benchmarking.
        uid = session["user_id"]
        session_id = f"{run_id}-{s_index}"  # ONE id for the whole conversation
        records: list[dict[str, Any]] = []
        for t_index, turn in enumerate(session.get("turns", [])[:realized_turns]):
            message = turn["message"]
            record: dict[str, Any] = {
                "index": s_index * realized_turns + t_index,
                "session": s_index,
                "turn": t_index,
                "user_id": uid,
                "message": message,
            }
            try:
                resp = agent_client.invoke(
                    message,
                    uid,
                    profile={"name": uid, "email": f"{uid}@benchmark.local", "tier": "basic"},
                    session_id=session_id,  # SAME session_id every turn
                    run_id=run_id,
                    flags=flags,
                    usage_plans=no_caps_plans,
                )
                record["reply"] = resp.get("reply", "")
                record["usage"] = resp.get("usage", {})
            except AgentError as exc:
                record["error"] = str(exc)
                record["usage"] = {}
            records.append(record)
            # Report once per completed turn (success OR error) so the progress
            # bar advances turn-by-turn across the parallel sessions.
            _report_turn_done()
        return records

    with ThreadPoolExecutor(max_workers=config.BENCHMARK_CONCURRENCY) as pool:
        futures = {pool.submit(_run_session, i, s): i for i, s in enumerate(selected)}
        for fut in as_completed(futures):
            s_index = futures[fut]
            session_results[s_index] = fut.result()

    # Flatten session->turn records in a deterministic (session, turn) order.
    records: list[dict[str, Any]] = [rec for recs in session_results for rec in recs]
    return records, realized_sessions, realized_turns
