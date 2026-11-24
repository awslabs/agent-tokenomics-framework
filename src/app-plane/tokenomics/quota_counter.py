"""Per-user token counters for the in-loop daily/monthly quota + TPM throttle.

The per-user quota (`daily_max_tokens_per_user` / `monthly_max_tokens_per_user`)
and the per-user throttle (`max_tokens_per_minute_per_user`) span many sessions
and turns, and each turn may run in a fresh AgentCore microVM, so the running
totals live in the `Tokenomics-EnforcementCounter` table (shared with the
session counter) with date/minute-keyed rows + TTL so each window resets
implicitly -- no rollover Lambda:

    USER#<sub>#DAY#<YYYY-MM-DD>   -> tokens spent today     (TTL ~a few days)
    USER#<sub>#MONTH#<YYYY-MM>    -> tokens spent this month (TTL ~a few weeks)
    USER#<sub>#MIN#<epoch_min>    -> tokens in this minute   (TTL ~minutes)

Policy (see ADR-001 integrity rules): the quota/throttle PRE-CHECK is
**fail-CLOSED** (a read error raises `CounterUnavailable` so the hook blocks the
turn -- a money limit must never let an unmetered turn through); the post-turn
increment is best-effort. The shared mechanics live in `_counter_store`; this
module owns only the `USER#` key shapes, the per-window TTLs, and the fail-closed
read policy.
"""

from __future__ import annotations

from datetime import datetime

import config
from tokenomics import _counter_store

# Re-exported so callers (the quota hook) can keep catching
# `quota_counter.CounterUnavailable` without knowing about `_counter_store`.
CounterUnavailable = _counter_store.CounterUnavailable

# Monthly rows must outlive a whole calendar month; the global TTL (default 3d)
# is tuned for session/daily rows. Floor the monthly TTL so a month's counter
# never expires mid-month.
_MONTHLY_TTL_DAYS = 35
# A per-minute throttle window only needs to survive a couple of minutes.
_MINUTE_TTL_SECONDS = 180


def _now() -> datetime:
    return _counter_store.now()


# ---- key shapes (one row per user per window) ------------------------------


def day_key(user_id: str, now: datetime | None = None) -> str:
    now = now or _now()
    return f"USER#{user_id}#DAY#{now.strftime('%Y-%m-%d')}"


def month_key(user_id: str, now: datetime | None = None) -> str:
    now = now or _now()
    return f"USER#{user_id}#MONTH#{now.strftime('%Y-%m')}"


def minute_key(user_id: str, now: datetime | None = None) -> str:
    now = now or _now()
    return f"USER#{user_id}#MIN#{int(now.timestamp()) // 60}"


# ---- reads (fail-CLOSED -- a money cap must not under-enforce) --------------


def read_tokens(pk: str) -> int:
    """Read one counter row's `tokens`, fail-CLOSED (raises on store error)."""
    return _counter_store.read_tokens(pk, fail_closed=True)


def read_day(user_id: str) -> int:
    return read_tokens(day_key(user_id))


def read_month(user_id: str) -> int:
    return read_tokens(month_key(user_id))


def read_minute(user_id: str) -> int:
    return read_tokens(minute_key(user_id))


# ---- increments (best-effort, fire-and-forget) -----------------------------


def add_tokens(pk: str, tokens: int, ttl_seconds: int) -> None:
    _counter_store.add_tokens(pk, tokens, ttl_seconds)


def add_day(user_id: str, tokens: int) -> None:
    add_tokens(day_key(user_id), tokens, int(config.ENFORCEMENT_COUNTER_TTL_DAYS) * 86400)


def add_month(user_id: str, tokens: int) -> None:
    add_tokens(month_key(user_id), tokens, _MONTHLY_TTL_DAYS * 86400)


def add_minute(user_id: str, tokens: int) -> None:
    add_tokens(minute_key(user_id), tokens, _MINUTE_TTL_SECONDS)
