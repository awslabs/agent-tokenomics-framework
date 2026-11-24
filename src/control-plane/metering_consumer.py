"""Metering consumer -- control-plane EventBridge target.

Receives `UsageRecorded` events emitted by the app plane (one per turn) and
persists them to the control-plane metrics/ledger table. This is where ALL
aggregation lives, so the app plane stays lean.

Wiring: an EventBridge rule (source=tokenomics.appplane, detail-type=UsageRecorded)
targets this Lambda (`metering_consumer.handler`). The rule delivers each event's
`detail` as the invocation payload.

Writes, per event:
  - roll-up counters (atomic ADD) for:
      SESSION#<session_id>
      USER#<user_id>#DAY#<yyyy-mm-dd>
      AGENT#<name>#DAY#<yyyy-mm-dd>   (one per agent in the by_agent breakdown)
      MODEL#<model_id>#USER#<user_id>#DAY#<yyyy-mm-dd>  (Advisor, R9a)
  - a per-session turn ledger row TURNS#<session_id> (Advisor, R10)
  - an enforcement event row (EVENT#<ts>#<uuid>) IFF governance acted this turn.

The consumer is idempotent-friendly (atomic ADD) and resilient: a bad event is
logged and skipped rather than failing the batch.
"""

from __future__ import annotations

# --- import bootstrap: make `lib`, `config`, `shared` importable (matches lambda_entry) ---
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
# -----------------------------------------------------------------------------------------

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from lib import store
from shared.logging_setup import configure_logging

configure_logging()  # Lambda root handler: set our level so INFO logs reach CloudWatch

logger = logging.getLogger(__name__)


def _day(ts_iso: str) -> str:
    try:
        return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(UTC).strftime("%Y-%m-%d")


def _detail(event: dict[str, Any]) -> dict[str, Any]:
    """EventBridge delivers the event; `detail` holds our payload. Support both
    the rule-target shape ({..., 'detail': {...}}) and a raw-detail invocation."""
    if isinstance(event, dict) and "detail" in event and isinstance(event["detail"], dict):
        return event["detail"]
    return event or {}


def _record_usage(detail: dict[str, Any]) -> None:
    """UsageRecorded -> session/user/agent token+cost roll-ups (tagged user_id).

    Source split (see Governance = live-only, benchmark lives in Usage & Cost
    Analytics): a turn carrying a `run_id` is BENCHMARK traffic -- it never
    counts toward a user's governance quota, so it must not pollute the live
    per-user / per-session / per-agent roll-ups the Governance page reads.
    Benchmark turns are therefore recorded (never dropped -- the customer still
    pays) into SEPARATE `BENCH#...` roll-up rows, keyed by run_id, so:
      - live rows (USER#/SESSION#/AGENT#)  -> Governance analytics (live-only)
      - BENCH# rows (per run / per agent)  -> Usage & Cost Analytics page
    """
    user_id = detail.get("user_id") or "unknown"
    session_id = detail.get("session_id") or "unknown"
    run_id = (detail.get("run_id") or "").strip()
    is_benchmark = bool(run_id)
    ts = detail.get("ts") or datetime.now(UTC).isoformat()
    day = _day(ts)
    usage = detail.get("usage") or {}
    total_tokens = int(usage.get("total_input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
    cost = float(usage.get("cost_usd", 0.0) or 0.0)
    by_agent = usage.get("by_agent") or {}

    if is_benchmark:
        # Benchmark roll-ups: per-run total + per-run-per-agent. Tagged source so
        # the Usage & Cost Analytics page can attribute spend to each run.
        store.add_metric_usage(
            f"BENCH#RUN#{run_id}",
            total_tokens,
            cost,
            {"ts": ts, "source": "benchmark", "run_id": run_id},
        )
        for name, a in by_agent.items():
            a_tokens = int(a.get("total_input_tokens", 0) or 0) + int(a.get("output_tokens", 0) or 0)
            a_cost = float(a.get("cost_usd", 0.0) or 0.0)
            store.add_metric_usage(
                f"BENCH#RUN#{run_id}#AGENT#{name}",
                a_tokens,
                a_cost,
                {"ts": ts, "source": "benchmark", "run_id": run_id, "agent": name},
            )
        return

    # ---- live user traffic (feeds the Governance analytics, live-only) ----
    # `split` (R9b) carries the per-bucket token counts the Advisor's cache
    # analysis needs; `tokens`/`cost_usd` are written exactly as before.
    split = _split(usage)
    # Session roll-up (avg tokens/session, avg $/session, total sessions, turns).
    store.add_metric_usage(
        f"SESSION#{session_id}",
        total_tokens,
        cost,
        {"ts": ts, "user_id": user_id, "source": "live", "split": split},
        turns=1,
    )
    # Per-user-per-day roll-up (usage-vs-cap, near-limit, cost).
    store.add_metric_usage(
        f"USER#{user_id}#DAY#{day}",
        total_tokens,
        cost,
        {"ts": ts, "user_id": user_id, "day": day, "source": "live", "split": split},
        turns=1,
    )
    # Per-agent roll-ups from the by_agent breakdown. Keyed per-agent-PER-USER-per-day
    # so the Cost-by-agent widget can be filtered by user AND summed for "all users"
    # — a plain AGENT#<name>#DAY row mixes every user's spend into one row (its
    # user_id tag would only be the last writer), which cannot be user-filtered.
    for name, a in by_agent.items():
        a_tokens = int(a.get("total_input_tokens", 0) or 0) + int(a.get("output_tokens", 0) or 0)
        a_cost = float(a.get("cost_usd", 0.0) or 0.0)
        store.add_metric_usage(
            f"AGENT#{name}#USER#{user_id}#DAY#{day}",
            a_tokens,
            a_cost,
            {
                "ts": ts,
                "user_id": user_id,
                "day": day,
                "agent": name,
                "source": "live",
                "model_id": _model_id(a),
                "split": _split(a),
            },
        )

    # ---- Advisor enrichment (additive; written after the roll-ups above) ----
    # Each write is isolated so a failure here can never drop the existing rows.
    _record_models(user_id, day, ts, by_agent)
    try:
        _record_turn(detail, session_id, ts, total_tokens, cost, by_agent)
    except Exception as exc:  # noqa: BLE001 -- additive metric, never fail the event
        logger.warning("[metering-consumer] turn ledger skipped (%s: %s)", type(exc).__name__, exc)


def _split(u: dict[str, Any]) -> dict[str, int]:
    """Per-bucket token counts from a usage dict (turn total or one agent)."""
    return {attr: int(u.get(attr, 0) or 0) for attr in store.SPLIT_ATTRS}


def _model_id(a: dict[str, Any]) -> str:
    """The agent's model id, or "" if absent / not safe to embed in a key."""
    mid = str(a.get("model_id") or "")
    return mid if mid and "#" not in mid else ""


def _record_models(user_id: str, day: str, ts: str, by_agent: dict[str, Any]) -> None:
    """R9a: MODEL#<model_id>#USER#<sub>#DAY#<d> roll-ups (user embedded so the
    model breakdown can be user-filtered, same reason as the AGENT# key).
    Each write is isolated so one failure cannot drop the other agents' rows."""
    for a in by_agent.values():
        model_id = _model_id(a)
        if not model_id:
            continue
        a_tokens = int(a.get("total_input_tokens", 0) or 0) + int(a.get("output_tokens", 0) or 0)
        a_cost = float(a.get("cost_usd", 0.0) or 0.0)
        try:
            store.add_metric_usage(
                f"MODEL#{model_id}#USER#{user_id}#DAY#{day}",
                a_tokens,
                a_cost,
                {
                    "ts": ts,
                    "user_id": user_id,
                    "day": day,
                    "model_id": model_id,
                    "source": "live",
                    "split": _split(a),
                },
            )
        except Exception as exc:  # noqa: BLE001 -- additive metric, never fail the event
            logger.warning("[metering-consumer] model roll-up skipped (%s: %s)", type(exc).__name__, exc)


def _record_turn(
    detail: dict[str, Any],
    session_id: str,
    ts: str,
    total_tokens: int,
    cost: float,
    by_agent: dict[str, Any],
) -> None:
    """R10: append this turn to the per-session ledger row TURNS#<session_id>."""
    if not session_id or session_id == "unknown":
        return
    usage = detail.get("usage") or {}
    gov = detail.get("governance") or {}
    entry = {
        "ts": ts,
        "tokens": total_tokens,
        "cost_usd": cost,
        **_split(usage),
        "agents": {
            name: {
                "tokens": int(a.get("total_input_tokens", 0) or 0) + int(a.get("output_tokens", 0) or 0),
                "cost_usd": float(a.get("cost_usd", 0.0) or 0.0),
                "model_calls": int(a.get("model_calls", 0) or 0),
            }
            for name, a in by_agent.items()
        },
        "governance": {
            "reason": gov.get("reason") or "",
            "stopped": bool(gov.get("stopped")),
            "limited": bool(gov.get("limited")),
        },
    }
    if not store.append_turn(session_id, entry):
        logger.warning("[metering-consumer] turn ledger full for session %s", session_id)


def _record_violation(detail: dict[str, Any]) -> None:
    """LimitViolation -> per-config violation count roll-up + an event-log row."""
    user_id = detail.get("user_id") or "unknown"
    session_id = detail.get("session_id") or "unknown"
    ts = detail.get("ts") or datetime.now(UTC).isoformat()
    day = _day(ts)
    cfg = detail.get("config") or "governance"

    store.add_metric_count(
        f"VIOLATION#{cfg}#DAY#{day}",
        1,
        {"ts": ts, "user_id": user_id, "day": day, "config": cfg},
    )
    store.append_event(
        {
            "pk": f"EVENT#{ts}#{uuid.uuid4().hex[:8]}",
            "ts": ts,
            "type": detail.get("reason") or cfg,
            "config": cfg,
            "scope": detail.get("scope") or "turn",
            "agent": detail.get("agent"),
            "severity": "high" if detail.get("stopped") else "medium",
            "user_id": user_id,
            "session_id": session_id,
            "message": detail.get("detail") or "governance limit acted",
        }
    )


def _record_quota_block(detail: dict[str, Any]) -> None:
    """QuotaBlock -> per-config violation count + per-user-per-day count + event (403)."""
    user_id = detail.get("user_id") or "unknown"
    ts = detail.get("ts") or datetime.now(UTC).isoformat()
    day = _day(ts)
    cfg = detail.get("config") or "daily_max_tokens_per_user"
    store.add_metric_count(
        f"VIOLATION#{cfg}#DAY#{day}",
        1,
        {"ts": ts, "user_id": user_id, "day": day, "config": cfg},
    )
    # Per-user-per-day roll-up for the "Quota blocks (403) per day" chart
    # (mirrors THROTTLE#<sub>#DAY for the 429 chart).
    store.add_metric_count(
        f"QUOTABLOCK#{user_id}#DAY#{day}",
        1,
        {"ts": ts, "user_id": user_id, "day": day, "config": cfg},
    )
    store.append_event(
        {
            "pk": f"EVENT#{ts}#{uuid.uuid4().hex[:8]}",
            "ts": ts,
            "type": "quota",
            "config": cfg,
            "scope": "user",
            "severity": "high",
            "user_id": user_id,
            "message": detail.get("detail") or "per-user quota exceeded (403)",
        }
    )


def _record_throttle(detail: dict[str, Any]) -> None:
    """ThrottleEvent -> per-user-per-day throttle count + an event-log row (429)."""
    user_id = detail.get("user_id") or "unknown"
    ts = detail.get("ts") or datetime.now(UTC).isoformat()
    day = _day(ts)
    store.add_metric_count(
        f"THROTTLE#{user_id}#DAY#{day}",
        1,
        {"ts": ts, "user_id": user_id, "day": day, "config": "max_tokens_per_minute_per_user"},
    )
    store.append_event(
        {
            "pk": f"EVENT#{ts}#{uuid.uuid4().hex[:8]}",
            "ts": ts,
            "type": "throttle",
            "config": "max_tokens_per_minute_per_user",
            "scope": "throttling",
            "severity": "medium",
            "user_id": user_id,
            "message": detail.get("detail") or "per-user TPM throttle (429)",
        }
    )


# DetailType -> recorder.
_ROUTES = {
    "UsageRecorded": _record_usage,
    "LimitViolation": _record_violation,
    "QuotaBlock": _record_quota_block,
    "ThrottleEvent": _record_throttle,
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """EventBridge target. Routes by DetailType to the right recorder; never fails hard."""
    try:
        detail_type = event.get("detail-type") or event.get("DetailType") or "UsageRecorded"
        recorder = _ROUTES.get(detail_type, _record_usage)
        recorder(_detail(event))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 -- log + swallow so retries don't storm
        logger.warning("[metering-consumer] skip event (%s: %s)", type(exc).__name__, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
