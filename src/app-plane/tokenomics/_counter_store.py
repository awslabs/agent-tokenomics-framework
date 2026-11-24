"""Shared primitives for the app-plane enforcement counters.

Both the per-user quota counter (`quota_counter.py`) and the per-session counter
(`session_counter.py`) keep running token totals in the SAME
`Tokenomics-EnforcementCounter` DynamoDB table, using the identical mechanics:
one `get_item` to read, one atomic `ADD tokens ... SET ttl, last_ts` to write.
Only two things differ between them, and they are the governance-relevant part:

  * the KEY SHAPE (USER#..#DAY/MONTH/MIN vs SESSION#..) -- owned by each module, and
  * the READ FAILURE POLICY:
      - quota (a MONEY cap) is **fail-CLOSED**: a read error must raise so the
        hook blocks the turn (never let an unmetered turn through on a hiccup).
      - session (a UX guard) is **fail-OPEN**: a read error returns 0 so the cap
        is skipped rather than breaking the conversation.

This module holds ONLY the shared mechanics; each counter module stays a thin
wrapper that sets its key shape + its failure policy, so the fail-open/closed
distinction remains explicit and readable at each call site.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3

import config

logger = logging.getLogger(__name__)


class CounterUnavailable(RuntimeError):
    """Raised by a fail-CLOSED read when the counter row cannot be read."""


@lru_cache(maxsize=1)
def table():
    """The shared enforcement-counter DynamoDB table (cached per container)."""
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.ENFORCEMENT_COUNTER_TABLE)


def now() -> datetime:
    return datetime.now(UTC)


def ttl_epoch(seconds_from_now: int) -> int:
    """Absolute epoch TTL `seconds_from_now` in the future (DynamoDB TTL attr)."""
    return int(time.time()) + int(seconds_from_now)


def _num(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def read_tokens(pk: str, *, fail_closed: bool) -> int:
    """Read one counter row's `tokens`. A missing row is 0 (not an error).

    On a store error the behaviour is set by `fail_closed`:
      * True  (money cap)  -> raise `CounterUnavailable` so the caller blocks.
      * False (UX guard)   -> return 0 so the caller skips the cap (log first).
    """
    try:
        item = table().get_item(Key={"pk": pk}).get("Item")
    except Exception as exc:
        if fail_closed:
            raise CounterUnavailable(f"{type(exc).__name__}: {exc}") from exc
        logger.warning(
            "[counter-store] read failed for %s, failing open (%s: %s)", pk, type(exc).__name__, exc
        )
        return 0
    return _num((item or {}).get("tokens", 0))


def add_tokens(pk: str, tokens: int, ttl_seconds: int) -> None:
    """Atomically add `tokens` to a counter row (best-effort, fire-and-forget).

    Always best-effort: a dropped increment only slightly undercounts and the
    next read self-heals from the stored total, so an increment never breaks a
    turn (for either counter).
    """
    if tokens <= 0:
        return
    try:
        table().update_item(
            Key={"pk": pk},
            UpdateExpression="ADD tokens :t SET #ttl = :ttl, last_ts = :ts",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":t": int(tokens),
                ":ttl": ttl_epoch(ttl_seconds),
                ":ts": now().isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001 -- never break the turn on increment
        logger.warning("[counter-store] increment failed for %s (%s: %s)", pk, type(exc).__name__, exc)
