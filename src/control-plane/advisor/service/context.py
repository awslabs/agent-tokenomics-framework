"""Advisor context -- the named rules, one load of the stores per agent turn, input
resolution, and the row-reading building blocks every tool composes (see __init__.py)."""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any, NamedTuple

from advisor import WINDOWS
from handlers import governance
from lib import store

# ---- named rules (every threshold a tool applies; the signal thresholds are with the signals) --

DAILY_SPEND_OUTLIER_X = 10.0  # a user-day / agent-day is an outlier above 10x its peer median (A5)
SESSION_OUTLIER_X = 5.0  # a session is an outlier above 5x the peer average tokens per turn (A5)
MIN_PEERS = 2  # a median needs at least 2 peers to mean anything
NEAR_QUOTA_SHARE = 0.8  # "near quota" = at or above 80% of the cap (as governance.analytics)
TREND_FLAT_BAND_PCT = 5.0  # within +/-5% of the pro-rated previous period reads "flat"
CACHE_DROP_POINTS = 10.0  # a cache-hit drop = more than 10 points below the reference rate

TOP_USERS_SHOWN = 5  # get_cost_summary ranks this many users
CAP_USERS_SHOWN = 10  # get_plan_vs_actual lists this many users against the cap
CANDIDATES_SHOWN = 5  # an unknown/ambiguous user name suggests this many emails
EMAIL_LOOKUP_WINDOW = "30d"  # only users active this recently are looked up in Cognito
WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30}
FEATURE_PATTERNS = ("prompt_cache", "memory")  # optimization patterns reported as the user's active ones

# ---- sanitisation (tool output is data, never instructions) -----------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_SUB_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,128}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}$")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Shown in place of an id that fails validation. The row is kept (not dropped) so totals still
# reconcile with the Analytics page; only the id is replaced.
INVALID = "unrecognised id"


class AdvisorInputError(ValueError):
    """Bad tool input (unknown window, unknown/ambiguous user, bad id)."""

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


def _matching_or_invalid(value: Any, *patterns: re.Pattern[str]) -> str:
    text = str(value or "")
    return text if any(pattern.match(text) for pattern in patterns) else INVALID


def safe_email(value: Any) -> str:
    """An email (or Cognito sub) safe to show; anything else becomes INVALID."""
    return _matching_or_invalid(value, _EMAIL_RE, _SUB_RE)


def safe_session(value: Any) -> str:
    return _matching_or_invalid(value, _SESSION_RE)


def safe_model(value: Any) -> str:
    return _matching_or_invalid(value, _MODEL_RE)


def safe_agent(value: Any) -> str:
    return _matching_or_invalid(value, _AGENT_RE)


# ---- context: one load of the stores per agent turn ---------------------------


class AdvisorContext:
    """Live metric rows + governance config + flags, loaded once and shared by every tool call in
    a turn. `analytics()` results and the sub -> email map are cached on the context."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        governance_config: dict[str, Any],
        flags: dict[str, Any],
        get_row: Callable[[str], dict[str, Any] | None] | None = None,
    ):
        self.rows = [row for row in rows if not governance._is_benchmark_row(row)]
        # TURNS# ledger rows are not in list_metrics(); they are read per session.
        self._get_row = get_row or store.get_metric
        self.by_pk = {str(row.get("pk", "")): row for row in self.rows}
        self.governance_config = governance_config or {}
        self.flags = flags or {}
        self._analytics_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._emails_by_sub: dict[str, str] | None = None

    @classmethod
    def load(cls) -> AdvisorContext:
        return cls(store.list_metrics(), store.read_governance_config(), store.read_flags())

    def analytics(self, user: str, window: str) -> dict[str, Any]:
        """governance.analytics() for (email or 'all', window), computed once per context."""
        cache_key = (user, window)
        if cache_key not in self._analytics_cache:
            self._analytics_cache[cache_key] = governance.analytics(user=user, window=window)
        return self._analytics_cache[cache_key]

    def turn_row(self, session_id: str) -> dict[str, Any] | None:
        """The session's TURNS# per-turn ledger row (None when missing or benchmark traffic)."""
        row = self._get_row(f"TURNS#{session_id}")
        return None if row is None or governance._is_benchmark_row(row) else row

    def emails(self) -> dict[str, str]:
        """sub -> sanitised display email for every user with usage in the last EMAIL_LOOKUP_WINDOW
        (bounds the Cognito look-ups to recently active users)."""
        if self._emails_by_sub is None:
            recent_days = set(_window_days(EMAIL_LOOKUP_WINDOW))
            active_subs = {
                _key_id(pk) for pk in self.by_pk if pk.startswith("USER#") and _key_day(pk) in recent_days
            }
            self._emails_by_sub = {
                sub: safe_email(governance._email_for_sub(sub)) for sub in sorted(active_subs)
            }
        return self._emails_by_sub


class TurnContext:
    """One agent turn's context: loaded on first use and shared by that turn's (parallel)
    tool calls, so every call in a turn sees the same snapshot. Never shared across turns."""

    def __init__(self, loader: Callable[[], AdvisorContext] | None = None):
        self._loader = loader or AdvisorContext.load
        self._lock = threading.Lock()
        self._ctx: AdvisorContext | None = None

    def get(self) -> AdvisorContext:
        with self._lock:
            if self._ctx is None:
                self._ctx = self._loader()
            return self._ctx


# ---- input resolution --------------------------------------------------------

_USER_FORMAT_ERROR = "user must be 'all', an email, or a name"


class Scope(NamedTuple):
    """Who a tool call is about: the window, the user (sub None = everyone) and the data level."""

    window: str
    sub: str | None
    email: str
    level: str  # "user" for one user, "fleet" for everyone


def check_window(window: Any) -> str:
    """The window (default 7d); raises AdvisorInputError unless it is one of WINDOWS."""
    window = str(window or "7d")
    if window not in WINDOWS:
        raise AdvisorInputError(f"window must be one of {', '.join(WINDOWS)}")
    return window


def _resolve_scope(ctx: AdvisorContext, window: Any, user: Any) -> Scope:
    """The checked window and resolved user every per-user tool starts from."""
    window = check_window(window)
    sub, email = resolve_user(ctx, user)
    return Scope(window, sub, email, "user" if sub else "fleet")


def _name_of(email: str) -> str:
    return email.lower().split("@")[0]


def _subs_matching(emails: dict[str, str], user: str) -> list[str]:
    """Subs whose email matches `user`: the whole email; or else an exact name (the part before
    '@'), falling back to names that start with it. Raises on a malformed `user`."""
    if "@" in user:
        if not _EMAIL_RE.match(user):
            raise AdvisorInputError(_USER_FORMAT_ERROR)
        return [sub for sub, email in emails.items() if email.lower() == user.lower()]
    if not _NAME_RE.match(user):
        raise AdvisorInputError(_USER_FORMAT_ERROR)
    name = user.lower()
    exact = [sub for sub, email in emails.items() if _name_of(email) == name]
    return exact or [sub for sub, email in emails.items() if _name_of(email).startswith(name)]


def resolve_user(ctx: AdvisorContext, user: Any) -> tuple[str | None, str]:
    """'all' | email | bare name -> (sub or None, display email). Raises on unknown/ambiguous
    names, with up to CANDIDATES_SHOWN candidate emails when ambiguous."""
    user = str(user or "all").strip()
    if user.lower() == "all":
        return None, "all"
    emails = ctx.emails()
    matches = _subs_matching(emails, user)
    if not matches:
        raise AdvisorInputError("unknown or ambiguous user")
    if len(matches) > 1:
        raise AdvisorInputError(
            "unknown or ambiguous user", sorted(emails[sub] for sub in matches)[:CANDIDATES_SHOWN]
        )
    sub = matches[0]
    email = emails[sub]
    # analytics() filters by resolving the email itself; refuse to continue
    # if that would not land on the same user (it would silently show ALL).
    if email != "all" and governance._sub_for_email(email) != sub:
        raise AdvisorInputError("user resolution is unavailable right now")
    return sub, email


# ---- reading metric rows: the building blocks every tool composes ------------------

SECONDS_PER_DAY = 86400


def _now() -> datetime:
    return datetime.now(UTC)


def _window_days(window: str, periods_back: int = 0) -> list[str]:
    """The window's UTC days (YYYY-MM-DD), newest first. periods_back=1 gives the equal-length
    window just before it."""
    length = WINDOW_DAYS[window]
    today = _now().date()
    first = length * periods_back
    return [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(first, first + length)]


def _fraction_of_today_elapsed() -> float:
    now = _now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - midnight).total_seconds() / SECONDS_PER_DAY


# Metrics row keys (written by metering_consumer.py):
#   USER#<sub>#DAY#<d>   AGENT#<name>#USER#<sub>#DAY#<d>   MODEL#<model>#USER#<sub>#DAY#<d>
#   SESSION#<id>         TURNS#<session_id>
def _key_id(pk: str) -> str:
    """The id right after the key's kind prefix: the sub, agent name, model id or session id."""
    return pk.split("#", 2)[1]


def _key_day(pk: str) -> str:
    """The day of a per-day roll-up key ('' for keys without one)."""
    return pk.rsplit("#DAY#", 1)[-1] if "#DAY#" in pk else ""


def _model_of(pk: str, row: dict[str, Any]) -> str:
    return safe_model(row.get("model_id") or _key_id(pk))


# A group-by for _usage_totals_by / _group_rows: (key, row) -> group.
def _by_key_id(pk: str, row: dict[str, Any]) -> str:
    return _key_id(pk)


def _tokens(row: dict[str, Any]) -> int:
    return int(row.get("tokens", 0) or 0)


def _turns(row: dict[str, Any]) -> int:
    return int(row.get("turns", 0) or 0)


def _cost(row: dict[str, Any]) -> float:
    return float(row.get("cost_usd", 0.0) or 0.0)


def _day_rows(
    ctx: AdvisorContext, prefix: str, days: set[str], sub: str | None
) -> list[tuple[str, dict[str, Any]]]:
    """(key, row) for the per-user day roll-ups under `prefix` on `days`, for one user (sub) or all (None)."""
    matching = []
    for pk, row in ctx.by_pk.items():
        if not pk.startswith(prefix) or _key_day(pk) not in days:
            continue
        if "#USER#" not in pk and not pk.startswith("USER#"):
            continue  # day rows without a user segment (e.g. VIOLATION#) are not usage
        if sub and row.get("user_id") != sub and not pk.startswith(f"USER#{sub}#"):
            continue
        matching.append((pk, row))
    return matching


def _sessions(ctx: AdvisorContext, days: set[str], sub: str | None) -> list[tuple[str, dict[str, Any]]]:
    """(session id, SESSION# row) for sessions last active on `days` (a session without a
    timestamp is always included), for one user (sub) or all (None)."""
    matching = []
    for pk, row in ctx.by_pk.items():
        if not pk.startswith("SESSION#"):
            continue
        last_active = str(row.get("last_ts") or row.get("ts") or "")
        if last_active and last_active[:10] not in days:  # [:10] = the timestamp's YYYY-MM-DD day
            continue
        if sub and row.get("user_id") != sub:
            continue
        matching.append((_key_id(pk), row))
    return matching


def _group_rows(
    keyed_rows: Iterable[tuple[str, dict[str, Any]]], group_of: Callable[[str, dict[str, Any]], Any]
) -> dict[Any, list[dict[str, Any]]]:
    """Rows grouped by group_of(key, row), in their original order within each group."""
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for pk, row in keyed_rows:
        groups[group_of(pk, row)].append(row)
    return dict(groups)


def _usage_totals_by(
    ctx: AdvisorContext,
    prefix: str,
    days: set[str],
    sub: str | None,
    group_of: Callable[[str, dict[str, Any]], Any],
) -> dict[Any, dict[str, float]]:
    """Untruncated {tokens, cost} totals of the matching day rows, grouped by group_of(key, row)."""
    totals: dict[Any, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0})
    for pk, row in _day_rows(ctx, prefix, days, sub):
        group = totals[group_of(pk, row)]
        group["tokens"] += _tokens(row)
        group["cost"] += _cost(row)
    return totals


# ---- cache split (R9b): input / output / cache read / cache write tokens -----------


def _has_cache_split(row: dict[str, Any]) -> bool:
    return any(attr in row for attr in store.SPLIT_ATTRS)


def _rows_with_split(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if _has_cache_split(row)]


def _split_day_rows(
    ctx: AdvisorContext, prefix: str, days: set[str], sub: str | None
) -> list[dict[str, Any]]:
    """The day rows under `prefix` on `days` that carry the cache split."""
    return _rows_with_split(row for _, row in _day_rows(ctx, prefix, days, sub))


def _sum_token_split(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = dict.fromkeys(store.SPLIT_ATTRS, 0)
    for row in rows:
        for attr in store.SPLIT_ATTRS:
            totals[attr] += int(row.get(attr, 0) or 0)
    return totals


def _all_input_tokens(split_totals: dict[str, int]) -> int:
    return (
        split_totals["input_tokens"] + split_totals["cache_read_tokens"] + split_totals["cache_write_tokens"]
    )


def _cache_hit_pct(split_totals: dict[str, int]) -> float | None:
    """cache read / (input + cache read + cache write), as a percentage."""
    all_input = _all_input_tokens(split_totals)
    return (split_totals["cache_read_tokens"] / all_input * 100) if all_input else None


def _cache_hit_pct_of_rows(split_rows: list[dict[str, Any]]) -> float | None:
    """The hit rate over rows that carry the split (None when there are none)."""
    return _cache_hit_pct(_sum_token_split(split_rows)) if split_rows else None


# ---- small arithmetic shared by the tools ---------------------------------------


def _percent(part: float, whole: float) -> float | None:
    return (part / whole * 100) if whole else None


def _ratio(part: float, whole: float) -> float | None:
    return (part / whole) if whole else None


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def _cost_per_1k(cost: float, tokens: float) -> float | None:
    return (cost / tokens * 1000) if tokens else None


def _cheapest_unit_cost(unit_costs: dict[str, float]) -> float | None:
    """The lowest cost per 1k tokens, when there is more than one to compare and it is positive."""
    if len(unit_costs) < 2:
        return None
    cheapest = min(unit_costs.values())
    return cheapest if cheapest > 0 else None


def _user_email(ctx: AdvisorContext, sub: str) -> str:
    return ctx.emails().get(sub, INVALID)
