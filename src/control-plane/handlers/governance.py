"""/governance/config + /governance/analytics + /governance/events handlers.

Token-governance control-plane surface. The agent reads the same config store for
all in-loop enforcement, so a PUT here changes enforcement on the next turn (no
redeploy) -- exactly like /flags.

Kept thin: parse -> lib.store -> plain dicts, mirroring handlers/flags.py.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import boto3

import config
from lib import store

# Internal components that were once metered as if they were agents. Rows
# written before the fix (AGENT#summarizer#USER#...) are folded into the agent
# they serve, so per-agent cost still sums to the total and never lists a
# non-agent. New turns already count the summarizer inside the orchestrator.
_OWNING_AGENT = {"summarizer": "orchestrator"}


@lru_cache(maxsize=1)
def _cognito():
    return boto3.client("cognito-idp", region_name=config.AWS_REGION)


# ---- email <-> sub display resolution --------------------------------------


def _sub_if_exists(email: str) -> str | None:
    """The Cognito `sub` for `email`, or None ONLY if the user genuinely doesn't exist.

    `UserNotFoundException` is the only "not found": every other failure (unset
    pool, throttling, AccessDenied, ...) propagates, so a caller fails loudly
    instead of mistaking a transient Cognito error for an absent user (which would
    silently report zero usage for a real customer).
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    if not config.APP_USER_POOL_ID:
        raise ValueError("APP_USER_POOL_ID not configured; cannot resolve email")
    idp = _cognito()
    try:
        got = idp.admin_get_user(UserPoolId=config.APP_USER_POOL_ID, Username=email)
    except idp.exceptions.UserNotFoundException:
        return None
    for a in got.get("UserAttributes", []):
        if a["Name"] == "sub":
            return a["Value"]
    return None


def _resolve_sub(email: str) -> tuple[str, str]:
    """Resolve a customer email -> (sub, verified_email) via the app-plane pool.

    Raises ValueError (=> 400) if the pool isn't configured or the user isn't
    found; a real Cognito failure (throttling, access denied) propagates (=> 500)
    rather than masquerading as "not found". Used only by the per-user override path.
    """
    email = (email or "").strip().lower()
    sub = _sub_if_exists(email)
    if sub is None:
        raise ValueError(f"no such user: {email}")
    return sub, email


@lru_cache(maxsize=512)
def _email_for_sub(sub: str) -> str:
    """Best-effort sub -> email for display (cached). Falls back to the sub."""
    if not sub or not config.APP_USER_POOL_ID:
        return sub
    try:
        got = _cognito().admin_get_user(UserPoolId=config.APP_USER_POOL_ID, Username=sub)
        for a in got.get("UserAttributes", []):
            if a["Name"] == "email":
                return a["Value"]
    except Exception:
        pass
    return sub


def _sub_for_email(email: str) -> str | None:
    """Best-effort email -> sub for display/labels; None on ANY failure.

    Deliberately lenient (unlike `_sub_filter_for`): a label lookup must never
    fail a whole request, so a missing user OR a transient Cognito error both
    just yield None here.
    """
    if not email or email == "all":
        return None
    try:
        return _sub_if_exists(email)
    except Exception:  # noqa: BLE001 -- display helper: never fail the caller
        return None


def _sub_filter_for(user: str) -> str | None:
    """Resolve the request's `user` param to a sub FILTER for the analytics widgets.

    Only the explicit "all" (or an empty value) means "no filter -> every user".
    Any other value is resolved to a sub; if the user GENUINELY doesn't exist
    (e.g. the value is already a raw sub, or an orphaned metrics sub for a user
    who is gone), it is used verbatim as the filter -> its own (empty) slice,
    never the fleet. That distinction is the whole point: without it an
    unresolvable user falls through to the WHOLE fleet's totals, which leaks
    every user's totals under a single-user query and breaks the
    "sum of per-user == all-users" invariant.

    A real Cognito failure (throttling, access denied, unset pool) propagates
    from `_sub_if_exists` and fails the request, rather than silently reporting
    zero usage for a real user.
    """
    if not user or user == "all":
        return None
    sub = _sub_if_exists(user)  # None ONLY for a genuinely-absent user
    return sub if sub is not None else user


# ---- config ----------------------------------------------------------------


def get_config() -> dict[str, Any]:
    """GET /governance/config -> the current nested GovernanceConfig."""
    return store.read_governance_config()


def put_config(body: dict[str, Any]) -> dict[str, Any]:
    """PUT /governance/config -> merge the partial config over current, persist.

    Accepts a partial nested config (e.g. just one agent's limits, the session
    cap, or the user quota) so the UI can send only what changed.
    """
    current = store.read_governance_config()
    body = body or {}

    merged: dict[str, Any] = {
        "agents": {**current.get("agents", {}), **(body.get("agents", {}) or {})},
        "session_max_tokens": body.get("session_max_tokens", current.get("session_max_tokens", 0)),
        "user_default": body.get("user_default", current.get("user_default", {})),
        # user_overrides is dormant in V1 (not sent by the UI); preserve as-is.
        "user_overrides": {
            **current.get("user_overrides", {}),
            **(body.get("user_overrides", {}) or {}),
        },
    }
    return store.write_governance_config(merged)


def put_user_override(body: dict[str, Any]) -> dict[str, Any]:
    """PUT /governance/user-override -> DORMANT (seed for the tier feature).

    Not surfaced in the V1 UI; kept so the future tier feature can build on it.
    Body: { "email": str, "quota": {daily_max_tokens_per_user, ...} }.
    """
    body = body or {}
    sub, email = _resolve_sub(body.get("email", ""))
    quota = dict(body.get("quota") or {})
    quota["email"] = email

    current = store.read_governance_config()
    overrides = {**current.get("user_overrides", {}), sub: quota}
    merged = {
        "agents": current.get("agents", {}),
        "session_max_tokens": current.get("session_max_tokens", 0),
        "user_default": current.get("user_default", {}),
        "user_overrides": overrides,
    }
    return store.write_governance_config(merged)


# ---- analytics + events ----------------------------------------------------

_ALL_CONFIGS = [
    "max_output_tokens_per_model_call",
    "max_tool_calls",
    "max_iterations",
    "max_tokens_per_agent_call",
    "session_max_tokens",
    "daily_max_tokens_per_user",
    "monthly_max_tokens_per_user",
    "max_tokens_per_minute_per_user",
]


def _window_days(window: str) -> list[str]:
    """List of yyyy-mm-dd strings covered by 24h|7d|30d (inclusive of today)."""
    n = {"24h": 1, "7d": 7, "30d": 30}.get(window, 7)
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


# Token-cap configs whose VIOLATION# rows count toward the "Total violations" KPI.
# The per-user quota (daily/monthly) + TPM throttle are request DENIALS, not cap
# violations -- they have their own KPIs/trends, so they are excluded here.
_CAP_CONFIGS = {
    "max_output_tokens_per_model_call",
    "max_tool_calls",
    "max_iterations",
    "max_tokens_per_agent_call",
    "session_max_tokens",
}


def _widget_violations(by_pk, in_window, sub_filter) -> list[dict[str, Any]]:
    """Per-config in-loop violation counts (VIOLATION#<config>#DAY#<date> rows)."""
    violations = dict.fromkeys(_ALL_CONFIGS, 0)
    for pk, m in by_pk.items():
        if not str(pk).startswith("VIOLATION#") or not in_window(pk):
            continue
        cfg = m.get("config") or (pk.split("#")[1] if "#" in pk else "")
        if sub_filter and m.get("user_id") not in (None, sub_filter):
            continue
        if cfg in violations:
            violations[cfg] += int(m.get("count", 0) or 0)
    return [{"config": c, "count": n} for c, n in violations.items()]


def _widget_cost_by_agent(by_pk, in_window, sub_filter) -> list[dict[str, Any]]:
    """Live per-agent cost from AGENT#<name>#USER#<sub>#DAY#<date> rows (benchmark excluded)."""
    agent_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0})
    for pk, m in by_pk.items():
        if not str(pk).startswith("AGENT#") or "#USER#" not in pk or not in_window(pk):
            continue
        if _is_benchmark_row(m):
            continue
        if sub_filter and m.get("user_id") != sub_filter:
            continue
        name = _OWNING_AGENT.get(pk.split("#")[1], pk.split("#")[1])
        agent_totals[name]["tokens"] += int(m.get("tokens", 0) or 0)
        agent_totals[name]["cost"] += float(m.get("cost_usd", 0.0) or 0.0)
    out = [
        {"agent": n, "tokens": int(t["tokens"]), "cost": round(t["cost"], 6)} for n, t in agent_totals.items()
    ]
    out.sort(key=lambda a: a["cost"], reverse=True)
    return out


def _usage_by_user(by_pk, in_window, sub_filter) -> dict[str, dict[str, float]]:
    """Live per-user token+cost totals from USER#<sub>#DAY#<date> rows (benchmark excluded).

    Shared by the top-users widget, the usage-vs-cap widget, and the token/cost KPIs,
    so they always agree.
    """
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0})
    for pk, m in by_pk.items():
        if not str(pk).startswith("USER#") or "#DAY#" not in pk or not in_window(pk):
            continue
        if _is_benchmark_row(m):
            continue
        sub = pk.split("#")[1]
        if sub_filter and sub != sub_filter:
            continue
        totals[sub]["tokens"] += int(m.get("tokens", 0) or 0)
        totals[sub]["cost"] += float(m.get("cost_usd", 0.0) or 0.0)
    return totals


def _widget_top_users(usage_by_user) -> list[dict[str, Any]]:
    """Top 20 users by cost (from the shared per-user totals)."""
    out = [
        {"user": _email_for_sub(sub), "tokens": int(t["tokens"]), "cost": round(t["cost"], 6)}
        for sub, t in usage_by_user.items()
    ]
    out.sort(key=lambda u: u["cost"], reverse=True)
    return out[:20]


def _widget_denials_by_day(by_pk, in_window, sub_filter) -> tuple[list[dict], list[dict]]:
    """Per-user/day throttle (429) and quota-block (403) counts for the trend charts."""
    throttle_rows: list[dict[str, Any]] = []
    quota_block_rows: list[dict[str, Any]] = []
    for pk, m in by_pk.items():
        if not in_window(pk):
            continue
        s = str(pk)
        prefix = (
            "THROTTLE#"
            if s.startswith("THROTTLE#")
            else ("QUOTABLOCK#" if s.startswith("QUOTABLOCK#") else "")
        )
        if not prefix:
            continue
        parts = s.split("#")  # <PREFIX>#<sub>#DAY#<date>
        if len(parts) < 4 or (sub_filter and parts[1] != sub_filter):
            continue
        row = {"user": _email_for_sub(parts[1]), "day": parts[3], "count": int(m.get("count", 0) or 0)}
        (throttle_rows if prefix == "THROTTLE#" else quota_block_rows).append(row)
    return throttle_rows, quota_block_rows


def _widget_usage_vs_cap(usage_by_user, window_cap: int) -> tuple[list[dict], int]:
    """Per-user usage vs the window-matched cap, + the count of users at >=80% of it."""
    rows = [
        {
            "user": _email_for_sub(sub),
            "used": int(t["tokens"]),
            "cost": round(t["cost"], 4),
            "quota": window_cap,
        }
        for sub, t in usage_by_user.items()
        if t["tokens"] > 0
    ]
    rows.sort(key=lambda u: u["used"], reverse=True)
    near = sum(1 for u in rows if window_cap > 0 and (u["used"] / window_cap) >= 0.8)
    return rows, near


def _sessions_in_window(by_pk, dayset, sub_filter) -> list[dict[str, Any]]:
    """Live SESSION# rows in the window (filtered by last_ts, since they carry no #DAY#)."""

    def in_window(m: dict[str, Any]) -> bool:
        ts = str(m.get("last_ts") or m.get("ts") or "")
        return (ts[:10] in dayset) if ts else True

    return [
        m
        for pk, m in by_pk.items()
        if str(pk).startswith("SESSION#")
        and not _is_benchmark_row(m)
        and in_window(m)
        and (not sub_filter or m.get("user_id") == sub_filter)
    ]


def _build_kpis(usage_by_user, sessions, violations_list, throttle_rows, users_near_quota) -> dict[str, Any]:
    """Assemble the two KPI rows from the already-computed widget pieces."""
    n_sessions = len(sessions)
    total_session_tokens = sum(int(s.get("tokens", 0) or 0) for s in sessions)
    total_session_cost = sum(float(s.get("cost_usd", 0.0) or 0.0) for s in sessions)
    total_violations = sum(v["count"] for v in violations_list if v["config"] in _CAP_CONFIGS)
    quota_blocks = sum(
        v["count"]
        for v in violations_list
        if v["config"] in ("daily_max_tokens_per_user", "monthly_max_tokens_per_user")
    )
    return {
        "total_tokens": int(sum(int(t["tokens"]) for t in usage_by_user.values())),
        "total_cost_usd": round(sum(float(t["cost"]) for t in usage_by_user.values()), 4),
        "avg_tokens_per_session": int(total_session_tokens / n_sessions) if n_sessions else 0,
        "avg_cost_per_session": round(total_session_cost / n_sessions, 4) if n_sessions else 0.0,
        "total_violations": total_violations,
        "quota_blocks": quota_blocks,
        "throttles": sum(r["count"] for r in throttle_rows),
        "users_near_quota": users_near_quota,
        "total_sessions": n_sessions,
        "total_turns": sum(int(s.get("turns", 0) or 0) for s in sessions),
    }


def analytics(user: str = "all", window: str = "7d") -> dict[str, Any]:
    """GET /governance/analytics -> all widget data for the Governance page.

    Reads the metrics roll-ups once, then assembles each widget from a dedicated
    `_widget_*` helper (all filtered to the same window + user). `cost_by_agent`
    and `top_users` are returned for the separate Usage & Cost Analytics page,
    which reuses this endpoint.

    user  = "all" | an email (resolved to sub for filtering)
    window = 24h | 7d | 30d
    """
    dayset = set(_window_days(window))
    by_pk = {m.get("pk"): m for m in store.list_metrics()}
    sub_filter = _sub_filter_for(user)  # None ONLY for "all"; unknown user -> its own (empty) slice

    def in_window(pk: str) -> bool:
        # DAY-suffixed rows carry the date at the end; SESSION rows (no day) pass
        # here and are instead windowed by last_ts in `_sessions_in_window`.
        if "#DAY#" in pk:
            return pk.rsplit("#DAY#", 1)[-1] in dayset
        return True

    # The window-matched per-user cap (24h->daily, 7d->daily*7, 30d->monthly).
    ud = store.read_governance_config().get("user_default") or {}
    window_cap, cap_period = _window_cap(
        window,
        int(ud.get("daily_max_tokens_per_user", 0) or 0),
        int(ud.get("monthly_max_tokens_per_user", 0) or 0),
    )

    # Each widget from its own helper (all obey the window + user filter).
    violations_list = _widget_violations(by_pk, in_window, sub_filter)
    cost_by_agent = _widget_cost_by_agent(by_pk, in_window, sub_filter)
    usage_by_user = _usage_by_user(by_pk, in_window, sub_filter)  # shared by 3 widgets below
    top_users = _widget_top_users(usage_by_user)
    throttle_rows, quota_block_rows = _widget_denials_by_day(by_pk, in_window, sub_filter)
    usage_vs_cap, users_near_quota = _widget_usage_vs_cap(usage_by_user, window_cap)
    sessions = _sessions_in_window(by_pk, dayset, sub_filter)
    kpis = _build_kpis(usage_by_user, sessions, violations_list, throttle_rows, users_near_quota)

    return {
        "window": window,
        "user": user,
        "cap_period": cap_period,
        "window_cap": window_cap,
        "kpis": kpis,
        "violations": violations_list,
        "cost_by_agent": cost_by_agent,
        "top_users": top_users,
        "throttle_by_day": throttle_rows,
        "quota_block_by_day": quota_block_rows,
        "usage_vs_cap": usage_vs_cap,
        "events": _events(user, window, None),
    }


def _window_cap(window: str, daily: int, monthly: int) -> tuple[int, str]:
    """The cap (and its period label) that matches the selected time window.

    24h → the daily cap; 7d → daily×7 (the expected 7-day ceiling); 30d → the
    monthly cap. 0 if that cap isn't configured.
    """
    if window == "24h":
        return daily, "daily"
    if window == "30d":
        return monthly, "monthly"
    # default / 7d
    return (daily * 7 if daily else 0), "weekly"


def _is_benchmark_row(m: dict[str, Any]) -> bool:
    """A roll-up row is benchmark if explicitly tagged, or is a BENCH# row.

    Rows written before source-tagging have no `source` -> treated as live.
    """
    if str(m.get("pk", "")).startswith("BENCH#"):
        return True
    return m.get("source") == "benchmark"


def events(user: str = "all", window: str = "7d", config_filter: str | None = None) -> dict[str, Any]:
    """GET /governance/events -> filtered enforcement event log."""
    return {"events": _events(user, window, config_filter)}


def _events(user: str, window: str, config_filter: str | None) -> list[dict[str, Any]]:
    days = set(_window_days(window))
    sub_filter = _sub_filter_for(user)  # None ONLY for "all"; unknown user -> its own (empty) slice
    rows = store.list_events()
    out = []
    for e in rows:
        ts = str(e.get("ts", ""))
        day = ts[:10]
        if days and day not in days:
            continue
        if config_filter and e.get("config") not in (config_filter, None):
            if e.get("config") != config_filter:
                continue
        if sub_filter and e.get("user_id") not in (None, sub_filter):
            continue
        # Add a display email for user-subject rows.
        uid = e.get("user_id") or ""
        e = {
            **e,
            "subject": _email_for_sub(uid)
            if uid and "@" not in uid and "#" not in uid
            else (e.get("subject") or uid),
        }
        out.append(e)
    return out
