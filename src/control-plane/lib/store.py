"""DynamoDB store for control-plane state: feature flags + benchmark runs.

Tables:
  - Flags (DataStack): one item per feature -> { feature, enabled, params }. The
    agent reads this same table, so a PUT here changes agent behavior next turn.
  - BenchmarkRuns (ControlPlaneStack): ONE item per benchmark, keyed by run_id
    (the benchmark id), holding both nested runs + progress + comparison.
  - UsagePlans / Metrics: token-governance config + the metering ledger.

DynamoDB has no float type, so we round-trip floats <-> Decimal at the boundary
(`_to_dynamo` on write, `_from_dynamo` on read) and the handlers keep working in
plain floats.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

import config
from shared import dynamo
from shared.flags_schema import ALL_FEATURES, FlagConfig, default_flag_config
from shared.usage_plans_schema import GovernanceConfig, default_governance_config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _dynamodb():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION)


@lru_cache(maxsize=1)
def _flags_table():
    return _dynamodb().Table(config.FLAGS_TABLE)


@lru_cache(maxsize=1)
def _runs_table():
    return _dynamodb().Table(config.RUNS_TABLE)


@lru_cache(maxsize=1)
def _usage_plans_table():
    return _dynamodb().Table(config.USAGE_PLANS_TABLE)


@lru_cache(maxsize=1)
def _metrics_table():
    return _dynamodb().Table(config.METRICS_TABLE)


# ---- float <-> Decimal boundary -------------------------------------------


# The float<->Decimal boundary lives in shared/dynamo (one copy for both planes).
# Keep the module-local names as thin aliases so the call sites below are unchanged.
_to_dynamo = dynamo.to_dynamo
_from_dynamo = dynamo.to_plain


# ---- flags -----------------------------------------------------------------


def read_flags() -> dict[str, Any]:
    """Reassemble the per-feature items into the FlagConfig dict shape."""
    try:
        resp = _flags_table().scan()
    except Exception as exc:  # noqa: BLE001 -- degrade to baseline, but LOG first (see tokenomics degradation policy)
        logger.warning(
            "[store] flags read failed, returning all-off baseline (%s: %s)", type(exc).__name__, exc
        )
        return default_flag_config()
    by_feature = {item["feature"]: item for item in resp.get("Items", [])}
    out: dict[str, Any] = {}
    for feature in ALL_FEATURES:
        item = by_feature.get(feature, {})
        out[feature] = {
            "enabled": bool(item.get("enabled", False)),
            "params": _from_dynamo(dict(item.get("params", {}) or {})),
        }
    return out


def write_flags(flags: dict[str, Any]) -> dict[str, Any]:
    """Normalize + persist each feature as its own item; return the config."""
    normalized = FlagConfig.from_dict(flags).to_dict()
    table = _flags_table()
    for feature, cfg in normalized.items():
        table.put_item(
            Item=_to_dynamo(
                {
                    "feature": feature,
                    "enabled": bool(cfg.get("enabled", False)),
                    "params": cfg.get("params", {}) or {},
                }
            )
        )
    return normalized


# ---- benchmark runs --------------------------------------------------------


def list_runs() -> list[dict[str, Any]]:
    resp = _runs_table().scan()
    runs = [_from_dynamo(item) for item in resp.get("Items", [])]
    # Newest first by created_at.
    return sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True)


def get_run(run_id: str) -> dict[str, Any] | None:
    resp = _runs_table().get_item(Key={"run_id": run_id})
    item = resp.get("Item")
    return _from_dynamo(item) if item is not None else None


def upsert_run(run: dict[str, Any]) -> None:
    _runs_table().put_item(Item=_to_dynamo(run))


def claim_run_for_worker(run_id: str) -> bool:
    """Atomically claim a benchmark record for a worker; True iff this caller won.

    Async Lambda ("Event") deliveries can arrive more than once and failed async
    executions are retried, so the worker must guard against running the same
    benchmark twice. This stamps a ONE-TIME `worker_claimed_at` marker on the
    benchmark record with a conditional write that only succeeds on the FIRST
    delivery:

      * `status = :running` -- the benchmark has NOT already finished. A COMPLETED
        or FAILED record is terminal and can never be claimed. AND
      * `attribute_not_exists(worker_claimed_at)` -- no worker has claimed it yet.
        The claim is set ONCE and never cleared, so every later delivery (a
        concurrent duplicate OR an AWS async retry after a timeout) finds the
        marker present and is refused.

    This is a strict, non-reclaimable dedup: exactly one delivery ever enters the
    Run A -> Run B loop. We deliberately do NOT reclaim a benchmark from a worker
    that died mid-run (timeout / OOM). Replaying the run logic is NOT idempotent:
    the load generator derives per-session identities from the benchmark id and
    emits usage under `BENCH#RUN#<id>`, so re-running a partially-completed
    benchmark would reuse dirty AgentCore conversation memory and double-count
    completed turns. A worker killed mid-run therefore leaves its benchmark stuck
    RUNNING by design; recovering a stuck benchmark (a reaper, or an idempotent
    checkpointed re-run) is out of scope. The kickoff-failure path in
    start_benchmark still marks a never-started benchmark FAILED.

    Returns False when the claim is refused (a duplicate / retried delivery, or an
    already-terminal record).
    """
    now_iso = datetime.now(UTC).isoformat()
    try:
        _runs_table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET worker_claimed_at = :now",
            ConditionExpression=("#status = :running AND attribute_not_exists(worker_claimed_at)"),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":now": now_iso,
                ":running": "RUNNING",
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


# ---- usage plans / quotas (shared config: control writes, agent reads) -----


def read_governance_config() -> dict[str, Any]:
    """Scan the plans table and return the nested GovernanceConfig dict shape."""
    try:
        resp = _usage_plans_table().scan()
    except Exception as exc:  # noqa: BLE001 -- degrade to defaults, but LOG first (see tokenomics degradation policy)
        logger.warning(
            "[store] governance config read failed, returning defaults (%s: %s)", type(exc).__name__, exc
        )
        return default_governance_config()
    items = [_from_dynamo(i) for i in resp.get("Items", [])]
    return GovernanceConfig.from_items(items).to_dict()


def write_governance_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize the nested config + persist one item per plan_id; return it."""
    normalized = GovernanceConfig.from_dict(config)
    table = _usage_plans_table()
    for item in normalized.to_items():
        table.put_item(Item=_to_dynamo(item))
    return normalized.to_dict()


# ---- metrics / ledger (control-plane owned; EventBridge consumer writes) ---

# Per-bucket token attributes (R9b) written alongside the `tokens` total.
SPLIT_ATTRS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# Cap on per-session turn entries in one TURNS#<session_id> row (R10). An entry
# is ~300 bytes (ts, token split, cost, per-agent tokens/cost/model_calls,
# governance), so 200 entries stay ~60 KB -- far under DynamoDB's 400 KB item
# limit and cheap to rewrite on each append.
TURNS_MAX_ENTRIES = 200
# Ledger rows the analytics scans never need; list_metrics filters them out so
# per-session turn logs are not returned on every Analytics/Governance read.
_LEDGER_PREFIX = "TURNS#"


def list_metrics(prefix: str | None = None) -> list[dict[str, Any]]:
    """All metric roll-up rows (optionally filtered by pk prefix).

    Follows LastEvaluatedKey so the result is complete past DynamoDB's 1 MB
    per-page Scan limit (a single page would silently truncate the totals).
    The Advisor's TURNS#<session_id> ledger rows are excluded (read them with
    get_metric); no roll-up reader uses them.
    """
    table = _metrics_table()
    kwargs: dict[str, Any] = {
        "FilterExpression": "NOT begins_with(pk, :ledger)",
        "ExpressionAttributeValues": {":ledger": _LEDGER_PREFIX},
    }
    resp = table.scan(**kwargs)
    items = list(resp.get("Items", []))
    while resp.get("LastEvaluatedKey"):
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    rows = [_from_dynamo(i) for i in items]
    if prefix:
        rows = [r for r in rows if str(r.get("pk", "")).startswith(prefix)]
    return rows


def get_metric(pk: str) -> dict[str, Any] | None:
    resp = _metrics_table().get_item(Key={"pk": pk})
    item = resp.get("Item")
    return _from_dynamo(item) if item is not None else None


def add_metric_usage(
    pk: str,
    tokens: int,
    cost_usd: float,
    extra: dict[str, Any] | None = None,
    turns: int = 0,
) -> None:
    """Atomically accumulate token/$ (and optionally a turn count) into a roll-up.

    `pk` examples: SESSION#<id>, USER#<id>#DAY#<date>, AGENT#<name>#DAY#<date>,
    BENCH#RUN#<run_id>. `extra` may carry {ts, user_id, day, agent, source} —
    these are written as attributes so the analytics layer can filter/group by
    them. `turns` (>0) atomically increments a per-row turn counter (used for the
    "total turns" KPI on live SESSION#/USER# rows). `extra["split"]` (optional)
    additionally ADDs the per-bucket token counts (input/output/cache read/
    cache write) used by the Advisor's cache-health analysis; `tokens` is still
    written so existing readers are unaffected.
    """
    extra = extra or {}
    # `last_ts` is a reserved-safe name; dimension attrs go through #names.
    expr = "ADD tokens :t, cost_usd :c"
    vals: dict[str, Any] = {
        ":t": int(tokens),
        ":c": _to_dynamo(float(cost_usd)),
        ":ts": extra.get("ts", ""),
    }
    names: dict[str, str] = {}
    split = extra.get("split") or {}
    for attr in SPLIT_ATTRS:
        if attr in split:
            names[f"#{attr}"] = attr
            vals[f":{attr}"] = int(split.get(attr) or 0)
            expr += f", #{attr} :{attr}"
    if turns > 0:
        # `turns` is a DynamoDB reserved keyword -> escape via #turns.
        expr += ", #turns :turns"
        names["#turns"] = "turns"
        vals[":turns"] = int(turns)
    expr += " SET last_ts = :ts"
    expr = _apply_dimension_attrs(expr, vals, names, extra)
    kwargs: dict[str, Any] = {"Key": {"pk": pk}, "UpdateExpression": expr, "ExpressionAttributeValues": vals}
    if names:
        kwargs["ExpressionAttributeNames"] = names
    _metrics_table().update_item(**kwargs)


def add_metric_count(pk: str, count: int = 1, extra: dict[str, Any] | None = None) -> None:
    """Atomically increment a COUNT-only roll-up row (violations / throttles).

    `pk` examples: VIOLATION#<config>#DAY#<date>, THROTTLE#<sub>#DAY#<date>.
    Stores config/user_id/day attributes for analytics grouping.
    """
    extra = extra or {}
    # `count` is a DynamoDB reserved keyword -> escape via #c.
    expr = "ADD #c :n SET last_ts = :ts"
    vals: dict[str, Any] = {":n": int(count), ":ts": extra.get("ts", "")}
    names: dict[str, str] = {"#c": "count"}
    expr = _apply_dimension_attrs(expr, vals, names, extra)
    _metrics_table().update_item(
        Key={"pk": pk},
        UpdateExpression=expr,
        ExpressionAttributeValues=vals,
        ExpressionAttributeNames=names,
    )


def append_turn(session_id: str, entry: dict[str, Any]) -> bool:
    """Append one turn to the per-session ledger row TURNS#<session_id> (R10).

    One row per session (not per turn) so the Metrics table grows like the
    SESSION# rows, and analytics -- which counts every `SESSION#` row as a
    session -- never sees these. Entries are appended to the `turn_log` list,
    capped at TURNS_MAX_ENTRIES; the row's `ttl` is refreshed on every append.
    EventBridge may redeliver an event, so readers dedupe entries on `ts`.
    Returns False when the cap is reached (the turn is not recorded). The row is
    read only by key, so it carries no dimension attributes.
    """
    try:
        _metrics_table().update_item(
            Key={"pk": f"TURNS#{session_id}"},
            UpdateExpression=(
                "SET #log = list_append(if_not_exists(#log, :empty), :entry), last_ts = :ts, #ttl = :ttl"
            ),
            ConditionExpression="attribute_not_exists(#log) OR size(#log) < :max",
            ExpressionAttributeValues={
                ":empty": [],
                ":entry": [_to_dynamo(entry)],
                ":ts": entry.get("ts", ""),
                ":ttl": int(time.time()) + int(config.TURN_TTL_DAYS) * 86400,
                ":max": TURNS_MAX_ENTRIES,
            },
            ExpressionAttributeNames={"#log": "turn_log", "#ttl": "ttl"},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


# DynamoDB reserved keywords among our dimension attributes -> always escaped.
def _apply_dimension_attrs(
    expr: str,
    vals: dict[str, Any],
    names: dict[str, str],
    extra: dict[str, Any],
) -> str:
    """Append user_id/day/config/agent SET clauses using #name placeholders.

    All dimension attributes are written through ExpressionAttributeNames so
    reserved keywords like `day` never appear bare in the UpdateExpression.
    Returns the augmented UpdateExpression.
    """
    sets = []
    for attr in ("user_id", "day", "config", "agent", "source", "run_id", "model_id"):
        v = extra.get(attr)
        if v:
            nph = f"#{attr}"
            vph = f":{attr}"
            names[nph] = attr
            vals[vph] = v
            sets.append(f"{nph} = {vph}")
    if sets:
        joiner = ", " if " SET " in expr else " SET "
        expr = expr + joiner + ", ".join(sets)
    return expr


def append_event(event: dict[str, Any]) -> None:
    """Store one enforcement/usage event row (pk = EVENT#<ts>#<uuid>)."""
    _metrics_table().put_item(Item=_to_dynamo(event))


def list_events(limit: int = 100) -> list[dict[str, Any]]:
    """Enforcement events, newest first."""
    resp = _metrics_table().scan()
    events = [_from_dynamo(i) for i in resp.get("Items", []) if str(i.get("pk", "")).startswith("EVENT#")]
    events.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return events[:limit]
