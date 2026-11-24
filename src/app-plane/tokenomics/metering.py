"""Usage-event emission (metering) -- app plane side.

The app plane emits ONE `UsageRecorded` event per turn to EventBridge, then
forgets about it. It does NO aggregation, storage, or counting -- a control-plane
consumer owns all of that. This is the decoupling that keeps the app plane lean:
the agent publishes to a bus and does not even know the control plane exists.

Fire-and-forget + graceful: emission happens AFTER the reply is assembled and any
failure is swallowed (logged), so a metering hiccup or a missing bus never breaks
a customer turn. If `METERING_BUS` is empty, emission is skipped entirely.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

import config

logger = logging.getLogger(__name__)

_SOURCE = "tokenomics.appplane"


@lru_cache(maxsize=1)
def _client():
    return boto3.client("events", region_name=config.AWS_REGION)


def _emit(detail_type: str, detail: dict[str, Any]) -> None:
    """Publish one typed event to EventBridge. Never raises (fire-and-forget)."""
    if not config.METERING_BUS:
        return  # metering not configured -> app plane stays fully standalone
    detail = {**detail, "ts": detail.get("ts") or datetime.now(UTC).isoformat()}
    try:
        _client().put_events(
            Entries=[
                {
                    "Source": _SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail, default=str),
                    "EventBusName": config.METERING_BUS,
                }
            ]
        )
    except Exception as exc:  # noqa: BLE001 -- metering must never break a turn
        logger.warning("[metering] emit %s failed (%s: %s)", detail_type, type(exc).__name__, exc)


def emit_usage(
    *,
    user_id: str,
    session_id: str,
    run_id: str | None,
    flag_signature: str,
    usage: dict[str, Any],
    governance: dict[str, Any] | None,
    model_ids: dict[str, str] | None = None,
) -> None:
    """Publish one UsageRecorded event to EventBridge. Never raises.

    `usage` is `UsageCollector.to_dict()` (turn total + by_agent, tokens + $).
    `governance` is the per-turn enforcement outcome (stopped/limited/...).
    """
    _emit(
        "UsageRecorded",
        {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "flag_config": flag_signature,
            "usage": usage,
            "governance": governance or {},
            "model_ids": model_ids or {},
        },
    )


def emit_governance_event(
    *,
    user_id: str,
    session_id: str,
    governance: dict[str, Any],
) -> None:
    """Publish the right typed event when an in-loop governance action acted.

    Routes by the outcome `reason` so the control-plane consumer rolls each into
    the correct analytics bucket (matching its DetailType recorders):
      - per-user quota    (reason=quota)    -> QuotaBlock    (403, VIOLATION# + log)
      - per-user throttle (reason=throttle) -> ThrottleEvent (429, THROTTLE# + log)
      - every in-loop cap (iteration/tool/token/session/budget) -> LimitViolation
    All carry the config name that fired so per-config roll-ups are accurate.
    """
    gov = governance or {}
    reason = gov.get("reason")
    base = {
        "user_id": user_id,
        "session_id": session_id,
        "config": gov.get("config"),
        "scope": gov.get("scope"),
        "agent": gov.get("agent"),
        "reason": reason,
        "detail": gov.get("detail"),
        "stopped": gov.get("stopped"),
        "limited": gov.get("limited"),
    }
    if reason == "quota":
        _emit("QuotaBlock", base)
    elif reason == "throttle":
        _emit("ThrottleEvent", base)
    else:
        _emit("LimitViolation", base)
