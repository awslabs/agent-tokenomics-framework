"""The eight Advisor tools, each preceded by its own helpers, then the signal rules
(`_<tool>_signals`, registered in SIGNAL_RULES) and the TOOLS registry (see __init__.py)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from statistics import median
from typing import Any

from .context import (
    _AGENT_RE,
    _SESSION_RE,
    CACHE_DROP_POINTS,
    CAP_USERS_SHOWN,
    DAILY_SPEND_OUTLIER_X,
    FEATURE_PATTERNS,
    MIN_PEERS,
    NEAR_QUOTA_SHARE,
    SESSION_OUTLIER_X,
    TOP_USERS_SHOWN,
    TREND_FLAT_BAND_PCT,
    AdvisorContext,
    AdvisorInputError,
    _all_input_tokens,
    _by_key_id,
    _cache_hit_pct,
    _cache_hit_pct_of_rows,
    _cheapest_unit_cost,
    _cost,
    _cost_per_1k,
    _day_rows,
    _fraction_of_today_elapsed,
    _group_rows,
    _has_cache_split,
    _key_day,
    _key_id,
    _median_or_none,
    _model_of,
    _percent,
    _ratio,
    _resolve_scope,
    _rows_with_split,
    _sessions,
    _split_day_rows,
    _sum_token_split,
    _tokens,
    _turns,
    _usage_totals_by,
    _user_email,
    _window_days,
    check_window,
    resolve_user,
    safe_agent,
    safe_email,
    safe_model,
    safe_session,
)
from .figures import FIGURE_DECIMALS, Figures, _agent_key, _figure_key, _model_key
from .formatting import model_label
from .signals import SIGNAL_RULES, _period_comparison_signals


def _tool_result(
    tool: str,
    data_level: str,
    figures: Figures,
    labels: dict[str, Any] | None = None,
    note: str | None = None,
    available: bool = True,
    reason: str | None = None,
) -> dict[str, Any]:
    """The shape every tool returns. Signals come from the tool's SIGNAL_RULES entry, if it has one."""
    labels = labels or {}
    result: dict[str, Any] = {
        "tool": tool,
        "available": available,
        "data_level": data_level,
        "figures": figures.items if available else {},
        "labels": labels,
    }
    signal_rule = SIGNAL_RULES.get(tool)
    if available and signal_rule:
        result["signals"] = signal_rule(figures.items, labels)
    if note:
        result["note"] = note
    if reason:
        result["reason"] = reason
    return result


def _unavailable_result(tool: str, data_level: str, reason: str) -> dict[str, Any]:
    """A tool result with no figures, saying why the data is not there yet."""
    return _tool_result(tool, data_level, Figures(), available=False, reason=reason)


# ---- tools -------------------------------------------------------------------

_NO_MODEL_ROLLUPS_NOTE = "by-model figures need the R9a model roll-ups (recorded for turns after deployment)"


def _add_kpi_figures(figures: Figures, kpis: dict[str, Any], level: str) -> None:
    """The window's totals and per-session averages exactly as governance.analytics() reports them, so they
    reconcile with the Analytics page. The cost and token totals are about `level` (user or fleet)."""
    figures.add("total.cost", float(kpis["total_cost_usd"]), "usd", "Total cost", level, headline=True)
    figures.add("total.tokens", int(kpis["total_tokens"]), "tokens", "Total tokens", level, headline=True)
    figures.add("total.sessions", int(kpis["total_sessions"]), "count:sessions", "Sessions", "session")
    figures.add("total.turns", int(kpis["total_turns"]), "count:turns", "Turns", "session")
    figures.add(
        "session.avg_cost", float(kpis["avg_cost_per_session"]), "usd", "Average cost per session", "session"
    )
    figures.add(
        "session.avg_tokens",
        int(kpis["avg_tokens_per_session"]),
        "tokens",
        "Average tokens per session",
        "session",
    )


def _add_per_turn_figures(figures: Figures, session_rows: list[dict[str, Any]]) -> None:
    """Average turns per session, and average cost and tokens per turn, over the window's SESSION# rows."""
    total_turns = sum(_turns(row) for row in session_rows)
    figures.add(
        "session.avg_turns",
        _ratio(total_turns, len(session_rows)),
        "dec:turns",
        "Average turns per session",
        "session",
    )
    if not total_turns:
        return  # no per-turn averages without turns (and the cost/token fields are not read)
    total_cost = sum(_cost(row) for row in session_rows)
    figures.add("turn.avg_cost", total_cost / total_turns, "usd", "Average cost per turn", "session")
    total_tokens = sum(_tokens(row) for row in session_rows)
    figures.add("turn.avg_tokens", total_tokens / total_turns, "tokens", "Average tokens per turn", "session")


def _latest_model_by_agent(agent_rows: list[tuple[str, dict[str, Any]]]) -> dict[str, str]:
    """agent name -> the sanitised model id of its most recently active day row that records one
    (on a last_ts tie, the later row wins)."""
    latest: dict[str, tuple[str, str]] = {}
    for pk, row in agent_rows:
        if not row.get("model_id"):
            continue
        agent_name = _key_id(pk)
        last_active = str(row.get("last_ts", ""))
        if agent_name not in latest or last_active >= latest[agent_name][0]:
            latest[agent_name] = (last_active, safe_model(row["model_id"]))
    return {agent_name: model_id for agent_name, (_, model_id) in latest.items()}


def _add_agent_output_figures(figures: Figures, agent_name: str, split_rows: list[dict[str, Any]]) -> None:
    """An agent's output tokens and their share of all its tokens, from the rows that carry the cache split."""
    split_totals = _sum_token_split(split_rows)
    output_tokens = split_totals["output_tokens"]
    figures.add(
        _agent_key(agent_name, "output_tokens"),
        output_tokens,
        "tokens",
        f"{agent_name} output tokens",
        "agent",
    )
    figures.add(
        _agent_key(agent_name, "output_share_pct"),
        _percent(output_tokens, sum(split_totals.values())),
        "pct",
        f"{agent_name} output share of tokens",
        "agent",
    )


def _add_agent_vs_cheapest_figures(figures: Figures, unit_costs: dict[str, float]) -> None:
    """Each agent's cost per 1k tokens as a multiple of the cheapest agent's (when there are two to compare)."""
    cheapest = _cheapest_unit_cost(unit_costs)
    if cheapest is None:
        return
    for agent_name, unit_cost in unit_costs.items():
        figures.add(
            _agent_key(agent_name, "cost_per_1k_x_vs_cheapest"),
            unit_cost / cheapest,
            "x",
            f"{agent_name} cost per 1k tokens vs the cheapest agent",
            "agent",
        )


def _add_agent_figures(
    figures: Figures,
    cost_by_agent: list[dict[str, Any]],
    latest_models: dict[str, str],
    rows_by_agent: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Cost, tokens, share of spend and cost per 1k tokens for each agent in analytics' cost_by_agent, plus
    output tokens where its day rows carry the cache split; then, after every agent's own figures, each
    one's cost per 1k tokens vs the cheapest agent's. Returns the agents label (each with its latest model)."""
    all_agents_cost = sum(float(row["cost"]) for row in cost_by_agent)
    agents: list[dict[str, Any]] = []
    unit_costs: dict[str, float] = {}
    for row in cost_by_agent:
        agent_name = safe_agent(row["agent"])
        cost, tokens = float(row["cost"]), int(row["tokens"])
        unit_cost = _cost_per_1k(cost, tokens)
        if unit_cost is not None:
            unit_costs[agent_name] = unit_cost
        figures.add(_agent_key(agent_name, "cost"), cost, "usd", f"{agent_name} cost", "agent", headline=True)
        figures.add(_agent_key(agent_name, "tokens"), tokens, "tokens", f"{agent_name} tokens", "agent")
        figures.add(
            _agent_key(agent_name, "share_pct"),
            _percent(cost, all_agents_cost),
            "pct",
            f"{agent_name} share of spend",
            "agent",
            headline=True,
        )
        figures.add(
            _agent_key(agent_name, "cost_per_1k"),
            unit_costs.get(agent_name),
            "usd",
            f"{agent_name} cost per 1k tokens",
            "agent",
        )
        split_rows = _rows_with_split(rows_by_agent.get(agent_name, []))
        if split_rows:
            _add_agent_output_figures(figures, agent_name, split_rows)
        model_id = latest_models.get(agent_name)
        agents.append(
            {"agent": agent_name, "model_id": model_id, "model": model_label(model_id) if model_id else None}
        )
    _add_agent_vs_cheapest_figures(figures, unit_costs)
    return agents


def _add_model_figures(
    figures: Figures, totals_by_model: dict[str, dict[str, float]]
) -> list[dict[str, str]]:
    """Cost, share of spend, cost per 1k tokens and its multiple of the cheapest model's, for each model, most
    expensive first. Returns the models label."""
    all_models_cost = sum(totals["cost"] for totals in totals_by_model.values())
    unit_costs = {
        model_id: _cost_per_1k(totals["cost"], totals["tokens"])
        for model_id, totals in totals_by_model.items()
        if totals["tokens"]
    }
    cheapest = _cheapest_unit_cost(unit_costs)
    models = []
    for model_id, totals in sorted(totals_by_model.items(), key=lambda item: item[1]["cost"], reverse=True):
        label = model_label(model_id)
        figures.add(
            _model_key(model_id, "cost"), totals["cost"], "usd", f"{label} cost", "model", headline=True
        )
        figures.add(
            _model_key(model_id, "share_pct"),
            _percent(totals["cost"], all_models_cost),
            "pct",
            f"{label} share of spend",
            "model",
        )
        figures.add(
            _model_key(model_id, "cost_per_1k"),
            unit_costs.get(model_id),
            "usd",
            f"{label} cost per 1k tokens",
            "model",
        )
        if cheapest is not None and model_id in unit_costs:
            figures.add(
                _model_key(model_id, "cost_per_1k_x_vs_cheapest"),
                unit_costs[model_id] / cheapest,
                "x",
                f"{label} cost per 1k tokens vs the cheapest model",
                "model",
            )
        models.append({"model_id": model_id, "model": label})
    return models


def _add_top_user_figures(
    figures: Figures, ctx: AdvisorContext, totals_by_user: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    """How many users spent anything, the median user cost, and the TOP_USERS_SHOWN biggest spenders' cost and
    multiple of that median. Returns the top_users label."""
    user_costs = [totals["cost"] for totals in totals_by_user.values() if totals["cost"] > 0]
    # 0.0 rather than None, so users.median_cost is still reported when nobody has spend yet.
    median_cost = median(user_costs) if user_costs else 0.0
    figures.add("users.count", len(user_costs), "count:users", "Users with spend", "user")
    figures.add("users.median_cost", median_cost, "usd", "Median user cost", "user")
    ranked = sorted(totals_by_user.items(), key=lambda item: item[1]["cost"], reverse=True)[:TOP_USERS_SHOWN]
    top_users = []
    for rank, (sub, totals) in enumerate(ranked, start=1):
        email = _user_email(ctx, sub)
        figures.add(_figure_key("top_user", rank, "cost"), totals["cost"], "usd", f"{email} cost", "user")
        figures.add(
            _figure_key("top_user", rank, "x_over_median"),
            _ratio(totals["cost"], median_cost),
            "x",
            f"{email} vs median user",
            "user",
        )
        top_users.append({"rank": rank, "user": email})
    return top_users


def get_cost_summary(ctx: AdvisorContext, window: str = "7d", user: str = "all") -> dict[str, Any]:
    """Where is the spend going? The window's totals, then the split by agent, by model and (fleet-wide) by user.

    Args:
        window: "24h", "7d" or "30d".
        user: "all" for the fleet, or an email / bare name for that one user.

    Figures: `total.{cost,tokens,sessions,turns}`; `session.avg_{cost,tokens,turns}`; `turn.avg_{cost,tokens}`;
    `agent.<name>.{cost,tokens,share_pct,cost_per_1k}`, plus `output_tokens` / `output_share_pct` where the
    cache split is recorded and `cost_per_1k_x_vs_cheapest` when two agents can be compared;
    `model.<model_id>.{cost,share_pct,cost_per_1k[,cost_per_1k_x_vs_cheapest]}`; and for the fleet only
    `users.{count,median_cost}` and `top_user.<rank>.{cost,x_over_median}`.
    Labels: window, user, agents [{agent, model_id, model}], models [{model_id, model}] and, for the fleet only,
    top_users [{rank, user}].

    Unavailable when no usage was recorded in the window; otherwise a note says the by-model figures
    need the R9a model roll-ups when the window has none.
    Raises AdvisorInputError for an unknown window or user.
    """
    scope = _resolve_scope(ctx, window, user)
    days = set(_window_days(scope.window))
    if not _has_window_data(ctx, days, scope.sub):
        return _unavailable_result("get_cost_summary", scope.level, _NO_WINDOW_DATA_REASON)
    analytics = ctx.analytics(scope.email, scope.window)
    figures = Figures()
    _add_kpi_figures(figures, analytics["kpis"], scope.level)
    _add_per_turn_figures(figures, [row for _, row in _sessions(ctx, days, scope.sub)])

    agent_rows = _day_rows(ctx, "AGENT#", days, scope.sub)
    agents = _add_agent_figures(
        figures,
        analytics["cost_by_agent"],
        _latest_model_by_agent(agent_rows),
        _group_rows(agent_rows, _by_key_id),
    )
    models = _add_model_figures(figures, _usage_totals_by(ctx, "MODEL#", days, scope.sub, _model_of))

    labels: dict[str, Any] = {"window": scope.window, "user": scope.email, "agents": agents, "models": models}
    if not scope.sub:
        totals_by_user = _usage_totals_by(ctx, "USER#", days, None, _by_key_id)
        labels["top_users"] = _add_top_user_figures(figures, ctx, totals_by_user)
    note = None if models else _NO_MODEL_ROLLUPS_NOTE
    return _tool_result("get_cost_summary", scope.level, figures, labels, note)


_PERIOD_COMPARISON_NOTE = (
    "previous period = the equal-length window immediately before; the current period "
    "includes today so far (a partial UTC day), so trends compare it with a pro-rated "
    "previous period"
)


def _prorate_factor(window_length: int, elapsed: float) -> float:
    """The share of the previous period to compare with when the current one ends `elapsed` of the
    way through today: (days - 1 + elapsed) / days."""
    return (window_length - 1 + elapsed) / window_length


def _change_vs_prorated_pct(current: float, previous: float, prorate_factor: float) -> float | None:
    """The change % of `current` vs the pro-rated previous cost (None when there is nothing to compare)."""
    if not previous or not prorate_factor:
        return None
    prorated = previous * prorate_factor
    return (current - prorated) / prorated * 100


def _trend(window: str, current: float, previous: float, change_vs_prorated: float | None) -> str:
    """'new', 'up', 'down' or 'flat' (within TREND_FLAT_BAND_PCT) vs the pro-rated previous period. A 24h
    window is all of today so far, so a fall (or no comparison) reads 'in progress' rather than 'down'."""
    if previous == 0:
        return "new" if current > 0 else "flat"
    if change_vs_prorated is None:
        trend = "flat"
    elif change_vs_prorated > TREND_FLAT_BAND_PCT:
        trend = "up"
    elif change_vs_prorated < -TREND_FLAT_BAND_PCT:
        trend = "down"
    else:
        trend = "flat"
    if window == "24h" and (change_vs_prorated is None or trend == "down"):
        return "in progress"
    return trend


def _add_change_figures(
    figures: Figures,
    key: str,
    label: str,
    level: str,
    current: float,
    previous: float,
    window: str,
    prorate_factor: float,
    headline: bool = False,
) -> tuple[str, float | None]:
    """Add <key>.current / .previous / .delta / .delta_pct and return (trend, change % vs the pro-rated
    previous period -- the raw change % when there is nothing to pro-rate)."""
    figures.add(_figure_key(key, "current"), current, "usd", f"{label} (this period)", level, headline)
    figures.add(_figure_key(key, "previous"), previous, "usd", f"{label} (previous period)", level)
    figures.add(_figure_key(key, "delta"), current - previous, "usd", f"{label} change", level, headline)
    change_pct = _percent(current - previous, previous)
    figures.add(_figure_key(key, "delta_pct"), change_pct, "pct", f"{label} change %", level, headline)
    change_vs_prorated = _change_vs_prorated_pct(current, previous, prorate_factor)
    trend = _trend(window, current, previous, change_vs_prorated)
    return trend, change_pct if change_vs_prorated is None else round(change_vs_prorated, FIGURE_DECIMALS)


def _total_cost(ctx: AdvisorContext, days: set[str], sub: str | None) -> float:
    """The cost of every user day row on `days` (0 when there are none). Added up row by row with +=, as
    _usage_totals_by does: Python 3.12's sum() compensates float error, which can move a total across a
    display boundary (101.49999999999999 -> "$101", 101.5 -> "$102")."""
    total = 0
    for _, row in _day_rows(ctx, "USER#", days, sub):
        total += _cost(row)
    return total


_NO_WINDOW_DATA_REASON = (
    "no usage was recorded in the selected window; pick a wider window "
    "(for example 30d) or send a few chat turns, then ask again"
)


def _has_window_data(ctx: AdvisorContext, days: set[str], sub: str | None) -> bool:
    """True when at least one live USER# day row falls in `days` (for this user, or anyone).

    This is the "is there anything to talk about this period?" guard. When it is False, a tool
    returns `_unavailable_result` instead of computing deltas/percentages against an empty period
    (which would read as a misleading -100% / negative change).
    """
    return any(True for _ in _day_rows(ctx, "USER#", days, sub))


def _group_costs(
    ctx: AdvisorContext,
    prefix: str,
    days: set[str],
    sub: str | None,
    group_of: Callable[[str, dict[str, Any]], Any],
) -> dict[Any, float]:
    """The cost of the matching day rows per group (a group with no rows is absent)."""
    totals = _usage_totals_by(ctx, prefix, days, sub, group_of)
    return {group: group_totals["cost"] for group, group_totals in totals.items()}


# How _compare_groups names a group: group -> (trend label key, figure key, figure label).
def _agent_comparison_names(agent_name: str) -> tuple[str, str, str]:
    agent_name = safe_agent(agent_name)
    return f"agent:{agent_name}", _agent_key(agent_name), f"{agent_name} cost"


def _model_comparison_names(model_id: str) -> tuple[str, str, str]:
    return f"model:{model_id}", _model_key(model_id), f"{model_label(model_id)} cost"


def _compare_groups(
    figures: Figures,
    current_costs: dict[str, float],
    previous_costs: dict[str, float],
    names_of: Callable[[str], tuple[str, str, str]],
    level: str,
    window: str,
    prorate_factor: float,
) -> dict[str, tuple[str, float | None]]:
    """Add the change figures of every group with cost in either period (in group order) and return
    trend key -> (trend, change % vs the pro-rated previous period). A group missing from one
    period costs 0.0 there."""
    compared = {}
    for group in sorted(set(current_costs) | set(previous_costs)):
        trend_key, key, label = names_of(group)
        compared[trend_key] = _add_change_figures(
            figures,
            key,
            label,
            level,
            current_costs.get(group, 0.0),
            previous_costs.get(group, 0.0),
            window,
            prorate_factor,
        )
    return compared


def get_period_comparison(ctx: AdvisorContext, window: str = "7d", user: str = "all") -> dict[str, Any]:
    """How did spend change from the previous period? The current window vs the equal-length window
    just before it, in total and per agent and model, plus the cache hit rate in both.

    Args:
        window: "24h", "7d" or "30d".
        user: "all" for the fleet, or an email / bare name for that one user.

    Figures: `total.{current,previous,delta,delta_pct}` (at the user or fleet level); the same four as
    `agent.<name>.*` and `model.<model_id>.*` for every agent / model with cost in either period; and
    `cache.hit_pct.{current,previous}`.
    Labels: window, elapsed_fraction_of_today, user, trend (total / agent:<name> / model:<model_id> ->
    new, up, down, flat or in progress), current_days and previous_days ([first, last]).
    Unavailable when the current period recorded no usage (nothing to compare). Otherwise signals are
    set here, not from SIGNAL_RULES, because the rule needs the total's change vs the pro-rated
    previous period, which is not one of the figures (delta_pct is the raw change).
    Raises AdvisorInputError for an unknown window or user.
    """
    scope = _resolve_scope(ctx, window, user)
    current_days, previous_days = set(_window_days(scope.window)), set(_window_days(scope.window, 1))
    # No usage this period: don't report a "change" against a non-empty previous period (it would
    # read as a misleading -100% / -$x drop). Say there is nothing to compare instead.
    if not _has_window_data(ctx, current_days, scope.sub):
        return _unavailable_result("get_period_comparison", scope.level, _NO_WINDOW_DATA_REASON)
    # Rows are per-day roll-ups and the current period includes today so far, so the raw figures
    # stay as they are; only the trend labels and signals compare against a pro-rated previous
    # period (a 24h window that would read "down" mid-day reads "in progress" instead).
    elapsed = round(_fraction_of_today_elapsed(), 3)
    prorate_factor = _prorate_factor(len(current_days), elapsed)
    figures = Figures()

    compared = {
        "total": _add_change_figures(
            figures,
            "total",
            "Total cost",
            scope.level,
            _total_cost(ctx, current_days, scope.sub),
            _total_cost(ctx, previous_days, scope.sub),
            scope.window,
            prorate_factor,
            headline=True,
        )
    }
    for prefix, group_of, names_of, level in (
        ("AGENT#", _by_key_id, _agent_comparison_names, "agent"),
        ("MODEL#", _model_of, _model_comparison_names, "model"),
    ):
        compared |= _compare_groups(
            figures,
            _group_costs(ctx, prefix, current_days, scope.sub, group_of),
            _group_costs(ctx, prefix, previous_days, scope.sub, group_of),
            names_of,
            level,
            scope.window,
            prorate_factor,
        )
    for period, days in (("current", current_days), ("previous", previous_days)):
        hit_pct = _cache_hit_pct_of_rows(_split_day_rows(ctx, "USER#", days, scope.sub))
        figures.add(
            _figure_key("cache.hit_pct", period),
            hit_pct,
            "pct",
            f"Cache hit rate ({period} period)",
            scope.level,
        )

    labels = {
        "window": scope.window,
        "elapsed_fraction_of_today": elapsed,
        "user": scope.email,
        "trend": {trend_key: trend for trend_key, (trend, _) in compared.items()},
        "current_days": [min(current_days), max(current_days)],
        "previous_days": [min(previous_days), max(previous_days)],
    }
    result = _tool_result("get_period_comparison", scope.level, figures, labels, _PERIOD_COMPARISON_NOTE)
    _, total_change_vs_prorated = compared["total"]
    result["signals"] = _period_comparison_signals(figures.items, total_change_vs_prorated)
    return result


_AGENT_NAME_ERROR = "agent must be an agent name such as orchestrator, knowledge or order"
_CACHE_SPLIT_MISSING = (
    "cache split (R9b) is not recorded for this window yet; it is captured for turns after "
    "the Advisor metering enrichment is deployed"
)
_PROMPT_CACHE_OFF = "; the prompt_cache flag is also OFF (enable it on the Feature flags page)"
_CACHE_HEALTH_NOTE = (
    f"cache hit = cache read / (input + cache read + cache write); a drop = more than "
    f"{CACHE_DROP_POINTS:g} points below the previous period (or the best earlier day in "
    "this window)"
)


def _check_agent_name(agent_name: str | None) -> None:
    """Raises AdvisorInputError unless `agent_name` is empty (every agent) or a valid agent name."""
    if agent_name is not None and agent_name != "" and not _AGENT_RE.match(str(agent_name)):
        raise AdvisorInputError(_AGENT_NAME_ERROR)


def _cache_unavailable_reason(prompt_cache_on: bool) -> str:
    """Why get_cache_health has no data, adding that the prompt_cache flag is off when it is."""
    return _CACHE_SPLIT_MISSING + ("" if prompt_cache_on else _PROMPT_CACHE_OFF)


def _add_cache_mix_figures(figures: Figures, split_totals: dict[str, int], level: str) -> None:
    """The headline cache figures: hit rate, read:write and the input share of all tokens."""
    all_input = _all_input_tokens(split_totals)
    figures.add("hit_pct", _cache_hit_pct(split_totals), "pct", "Cache hit rate", level, headline=True)
    figures.add(
        "read_write",
        _ratio(split_totals["cache_read_tokens"], split_totals["cache_write_tokens"]),
        "ratio",
        "Cache read:write",
        level,
        headline=True,
    )
    figures.add(
        "input_share_pct",
        _percent(all_input, all_input + split_totals["output_tokens"]),
        "pct",
        "Input share of tokens",
        level,
        headline=True,
    )


def _first_drop_day(daily_hit_pcts: dict[str, float | None], baseline: float | None) -> str | None:
    """The first day (in date order) whose hit rate is more than CACHE_DROP_POINTS below the reference:
    the previous period's rate (`baseline`), else the best earlier day in this window."""
    best_so_far: float | None = None
    for day in sorted(daily_hit_pcts):
        hit_pct = daily_hit_pcts[day]
        reference = baseline if baseline is not None else best_so_far
        if reference and hit_pct is not None and hit_pct < reference - CACHE_DROP_POINTS:
            return day
        if hit_pct is not None:
            best_so_far = hit_pct if best_so_far is None else max(best_so_far, hit_pct)
    return None


def get_cache_health(
    ctx: AdvisorContext, window: str = "7d", agent_name: str | None = None
) -> dict[str, Any]:
    """Is the prompt cache working? Hit rate, read:write, input share and the by-day hit rate, with
    the first day it dropped (R9b), for the whole fleet or one agent.

    Args:
        window: "24h", "7d" or "30d".
        agent_name: one agent (e.g. "orchestrator"); None or "" for every agent.

    Figures: `hit_pct`, `read_write`, `input_share_pct`, `coverage_pct` (share of tokens with cache data),
    `hit_pct_previous` and `day.<YYYY-MM-DD>.hit_pct`, at the agent or fleet level.
    Labels: window, agent ('all' for every agent), prompt_cache_enabled and first_drop_day (or None).
    Unavailable when no row in the window carries the cache split yet.
    Raises AdvisorInputError for an unknown window or a malformed agent name.
    """
    window = check_window(window)
    _check_agent_name(agent_name)
    level = "agent" if agent_name else "fleet"
    prefix = f"AGENT#{agent_name}#" if agent_name else "USER#"
    current_days, previous_days = set(_window_days(window)), set(_window_days(window, 1))
    day_rows = _day_rows(ctx, prefix, current_days, None)
    split_day_rows = [(pk, row) for pk, row in day_rows if _has_cache_split(row)]
    split_rows = [row for _, row in split_day_rows]
    prompt_cache_on = bool((ctx.flags.get("prompt_cache") or {}).get("enabled", False))
    if not split_rows:
        return _unavailable_result("get_cache_health", level, _cache_unavailable_reason(prompt_cache_on))

    figures = Figures()
    _add_cache_mix_figures(figures, _sum_token_split(split_rows), level)
    tokens_with_split = sum(_tokens(row) for row in split_rows)
    all_tokens = sum(_tokens(row) for _, row in day_rows)
    figures.add(
        "coverage_pct",
        _percent(tokens_with_split, all_tokens),
        "pct",
        "Share of tokens with cache data",
        level,
    )
    baseline = _cache_hit_pct_of_rows(_split_day_rows(ctx, prefix, previous_days, None))
    figures.add("hit_pct_previous", baseline, "pct", "Cache hit rate (previous period)", level)
    rows_by_day = _group_rows(split_day_rows, lambda pk, row: _key_day(pk))
    daily_hit_pcts = {day: _cache_hit_pct(_sum_token_split(rows_by_day[day])) for day in sorted(rows_by_day)}
    for day, hit_pct in daily_hit_pcts.items():
        figures.add(_figure_key("day", day, "hit_pct"), hit_pct, "pct", f"Cache hit rate on {day}", level)

    labels = {
        "window": window,
        "agent": agent_name or "all",
        "prompt_cache_enabled": prompt_cache_on,
        "first_drop_day": _first_drop_day(daily_hit_pcts, baseline),
    }
    return _tool_result("get_cache_health", level, figures, labels, _CACHE_HEALTH_NOTE)


# What get_outliers flags (the thresholds are the named rules above).
_OUTLIERS_NOTE = (
    f"user outlier: daily spend > {DAILY_SPEND_OUTLIER_X:g}× peer median; "
    f"agent outlier: a day > {DAILY_SPEND_OUTLIER_X:g}× that agent's median day; "
    f"session outlier: tokens/turn > {SESSION_OUTLIER_X:g}× peer average"
)


def _peer_spikes(values: list[tuple[str, float]]) -> list[tuple[str, float, float]]:
    """(member, value, median of the other members' values) for each member whose value is more than
    DAILY_SPEND_OUTLIER_X x that median. A median needs at least MIN_PEERS other members."""
    spikes = []
    for member, value in values:
        peer_values = [peer_value for peer, peer_value in values if peer != member]
        if len(peer_values) < MIN_PEERS:
            continue
        peer_median = median(peer_values)
        if peer_median > 0 and value > DAILY_SPEND_OUTLIER_X * peer_median:
            spikes.append((member, value, peer_median))
    return spikes


def _user_day_costs(ctx: AdvisorContext, days: set[str]) -> dict[str, list[tuple[str, float]]]:
    """day -> [(sub, that user's spend that day)], cheapest first: a user-day's peers are the other
    users on the same day, and a day's outliers are listed by spend."""
    totals = _usage_totals_by(ctx, "USER#", days, None, lambda pk, row: (_key_day(pk), _key_id(pk)))
    costs_by_day: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (day, sub), usage in sorted(totals.items(), key=lambda entry: entry[1]["cost"]):
        costs_by_day[day].append((sub, usage["cost"]))
    return dict(costs_by_day)


def _agent_day_costs(ctx: AdvisorContext, days: set[str]) -> dict[str, list[tuple[str, float]]]:
    """agent -> [(day, that agent's spend that day)], oldest day first. Agents have different roles (the
    orchestrator is always the biggest), so an agent-day's peers are that SAME agent's other days -- a
    spike, not a role."""
    totals = _usage_totals_by(ctx, "AGENT#", days, None, lambda pk, row: (_key_id(pk), _key_day(pk)))
    costs_by_agent: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (agent, day), usage in sorted(totals.items()):
        costs_by_agent[agent].append((day, usage["cost"]))
    return dict(costs_by_agent)


def _session_outliers(sessions: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, float, float]]:
    """(session id, tokens per turn, peer average) for each session whose tokens per turn are more than
    SESSION_OUTLIER_X x the other sessions' average. Sessions without turns are left out, and at least
    MIN_PEERS other sessions are needed."""
    counted = [(session_id, _tokens(row), _turns(row)) for session_id, row in sessions]
    with_turns = [(session_id, tokens, turns) for session_id, tokens, turns in counted if turns > 0]
    if len(with_turns) - 1 < MIN_PEERS:
        return []
    all_tokens = sum(tokens for _, tokens, _ in with_turns)
    all_turns = sum(turns for _, _, turns in with_turns)
    outliers = []
    for session_id, tokens, turns in with_turns:
        # Pooled over the other sessions (their tokens / their turns), not an average of their averages.
        peer_average = (all_tokens - tokens) / (all_turns - turns)
        tokens_per_turn = tokens / turns
        if peer_average > 0 and tokens_per_turn > SESSION_OUTLIER_X * peer_average:
            outliers.append((session_id, tokens_per_turn, peer_average))
    return outliers


def _add_outlier(
    figures: Figures,
    flagged: list[dict[str, Any]],
    kind: str,
    level: str,
    who: str,
    day: str,
    cost: float,
    peer_median: float,
    versus: str,
) -> None:
    """Add one flagged user-day or agent-day (`kind` "users" / "agents"), ranked after those already in
    `flagged`: its spend and its multiple of the peer median, which `versus` names."""
    rank = len(flagged) + 1
    figures.add(_figure_key(kind, rank, "cost"), cost, "usd", f"{who} spend on {day}", level, headline=True)
    figures.add(
        _figure_key(kind, rank, "x_over_median"), cost / peer_median, "x", versus, level, headline=True
    )
    flagged.append({"rank": rank, "who": who, "day": day})


def get_outliers(ctx: AdvisorContext, window: str = "7d") -> dict[str, Any]:
    """Which user-days, agent-days and sessions stand far above their peers in the window?

    A user-day is compared with the other users that day, an agent-day with that agent's other days
    (both: spend > DAILY_SPEND_OUTLIER_X x the peer median), and a session with the other sessions
    (tokens per turn > SESSION_OUTLIER_X x their average).

    Args:
        window: "24h", "7d" or "30d".

    Figures (ranks start at 1, in the order found): `users.<rank>.cost` and `users.<rank>.x_over_median`;
    `agents.<rank>.cost` and `agents.<rank>.x_over_median`; `sessions.<rank>.tokens_per_turn` and
    `sessions.<rank>.x_over_avg`; `count.users`, `count.agents` and `count.sessions`.
    Labels: window, users and agents ({rank, who, day}), sessions ({rank, session_id}).
    Always available (the counts are 0 when nothing stands out).
    Raises AdvisorInputError for an unknown window.
    """
    window = check_window(window)
    days = set(_window_days(window))
    figures = Figures()
    flagged: dict[str, list[dict[str, Any]]] = {"users": [], "agents": [], "sessions": []}

    user_day_costs = _user_day_costs(ctx, days)
    for day in sorted(user_day_costs):
        for sub, cost, peer_median in _peer_spikes(user_day_costs[day]):
            email = _user_email(ctx, sub)
            _add_outlier(
                figures,
                flagged["users"],
                "users",
                "user",
                email,
                day,
                cost,
                peer_median,
                f"{email} vs peer median on {day}",
            )
    agent_day_costs = _agent_day_costs(ctx, days)
    for agent in sorted(agent_day_costs):
        agent_name = safe_agent(agent)
        for day, cost, median_day_cost in _peer_spikes(agent_day_costs[agent]):
            _add_outlier(
                figures,
                flagged["agents"],
                "agents",
                "agent",
                agent_name,
                day,
                cost,
                median_day_cost,
                f"{agent_name} on {day} vs its median day",
            )

    session_outliers = _session_outliers(_sessions(ctx, days, None))
    for rank, (session_id, tokens_per_turn, peer_average) in enumerate(session_outliers, start=1):
        safe_id = safe_session(session_id)
        figures.add(
            _figure_key("sessions", rank, "tokens_per_turn"),
            tokens_per_turn,
            "tokens",
            f"Session {safe_id} tokens per turn",
            "session",
            headline=True,
        )
        figures.add(
            _figure_key("sessions", rank, "x_over_avg"),
            tokens_per_turn / peer_average,
            "x",
            f"Session {safe_id} vs peer average",
            "session",
            headline=True,
        )
        flagged["sessions"].append({"rank": rank, "session_id": safe_id})

    figures.add("count.users", len(flagged["users"]), "count:user-days", "User-day outliers", "user")
    figures.add("count.agents", len(flagged["agents"]), "count:agent-days", "Agent-day outliers", "agent")
    figures.add("count.sessions", len(flagged["sessions"]), "count:sessions", "Session outliers", "session")
    return _tool_result("get_outliers", "user", figures, {"window": window, **flagged}, _OUTLIERS_NOTE)


# get_user_profile compares these per-user metrics: metric -> (figure kind, its name in the signal text).
PROFILE_METRICS: dict[str, tuple[str, str]] = {
    "sessions": ("count:sessions", "sessions"),
    "turns": ("count:turns", "turns"),
    "tokens_per_turn": ("tokens", "tokens per turn"),
    "turns_per_session": ("dec:turns", "turns per session"),
    "cost_per_session": ("usd", "cost per session"),
    "cache_hit_pct": ("pct", "cache hit rate"),
}


def _by_user_id(key: str, row: dict[str, Any]) -> str:
    """Group-by for _group_rows: the row's user_id ('' when it has none)."""
    return str(row.get("user_id") or "")


def _usage_profile(
    sessions: list[dict[str, Any]], split_rows: list[dict[str, Any]]
) -> dict[str, float | None]:
    """One user's PROFILE_METRICS values from their SESSION# rows and their day rows that carry the
    cache split; a per-unit metric is None when there is nothing to divide by."""
    session_count = len(sessions)
    turns = sum(_turns(row) for row in sessions)
    tokens = sum(_tokens(row) for row in sessions)
    cost = sum(_cost(row) for row in sessions)
    return {
        "sessions": session_count,
        "turns": turns,
        "tokens_per_turn": _ratio(tokens, turns),
        "turns_per_session": _ratio(turns, session_count),
        "cost_per_session": _ratio(cost, session_count),
        "cache_hit_pct": _cache_hit_pct_of_rows(split_rows),
    }


def _add_profile_vs_median(
    figures: Figures,
    email: str,
    profile: dict[str, float | None],
    peer_profiles: list[dict[str, float | None]],
) -> None:
    """For each PROFILE_METRICS metric: the user's value, the peers' median, and the user's multiple of it."""
    for metric, (kind, name) in PROFILE_METRICS.items():
        value = profile[metric]
        peer_median = _median_or_none([peer[metric] for peer in peer_profiles if peer[metric] is not None])
        figures.add(_figure_key("user", metric), value, kind, f"{email} {name}", "user", headline=True)
        figures.add(_figure_key("median", metric), peer_median, kind, f"Median user {name}", "user")
        if value is not None and peer_median:
            figures.add(
                _figure_key("x_vs_median", metric),
                value / peer_median,
                "x",
                f"{email} {name} vs median",
                "user",
            )


def _add_agent_share_figures(figures: Figures, email: str, agent_totals: dict[str, dict[str, float]]) -> None:
    """Each agent's share of the user's spend, in agent-name order."""
    total_cost = sum(totals["cost"] for totals in agent_totals.values())
    for agent, totals in sorted(agent_totals.items()):
        agent_name = safe_agent(agent)
        figures.add(
            _agent_key(agent_name, "share_pct"),
            _percent(totals["cost"], total_cost),
            "pct",
            f"{email} {agent_name} share",
            "agent",
        )


def _active_patterns(flags: dict[str, Any]) -> dict[str, bool]:
    """Whether each FEATURE_PATTERNS feature flag is on."""
    return {p: bool((flags.get(p) or {}).get("enabled", False)) for p in FEATURE_PATTERNS}


def get_user_profile(ctx: AdvisorContext, window: str = "7d", user: str = "") -> dict[str, Any]:
    """How one user's usage compares with the median active user's (R3), and which optimization patterns are on.

    Args:
        window: "24h", "7d" or "30d".
        user: an email or a name ("all" or empty is refused: this tool is about one user).

    Figures, for each PROFILE_METRICS metric (sessions, turns, tokens_per_turn, turns_per_session,
    cost_per_session, cache_hit_pct): `user.<metric>`, `median.<metric>` (over the other users active in the
    window) and `x_vs_median.<metric>` (when both are there and the median is not 0); then
    `agent.<agent>.share_pct`, each agent's share of the user's spend.
    Labels: window, user, active_patterns ({flag: on} for FEATURE_PATTERNS) and peer_users (how many peers).
    Always available for a known user.
    Raises AdvisorInputError for an unknown window, a missing / 'all' user, or an unknown or ambiguous user.
    """
    window = check_window(window)
    if not user or str(user).lower() == "all":
        raise AdvisorInputError("get_user_profile needs a specific user (email or name)")
    sub, email = resolve_user(ctx, user)
    days = set(_window_days(window))

    # One pass over the rows, grouped by user (not one scan per user).
    sessions_by_user = _group_rows(_sessions(ctx, days, None), _by_user_id)
    day_rows_by_user = _group_rows(_day_rows(ctx, "USER#", days, None), _by_key_id)
    profile = _usage_profile(sessions_by_user.get(sub, []), _rows_with_split(day_rows_by_user.get(sub, [])))
    # Peers = other users ACTIVE in this window (idle users would drag the median to 0).
    peer_profiles = [
        _usage_profile(sessions, _rows_with_split(day_rows_by_user.get(peer_sub, [])))
        for peer_sub, sessions in sessions_by_user.items()
        if peer_sub and peer_sub != sub
    ]

    figures = Figures()
    _add_profile_vs_median(figures, email, profile, peer_profiles)
    _add_agent_share_figures(figures, email, _usage_totals_by(ctx, "AGENT#", days, sub, _by_key_id))
    labels = {
        "window": window,
        "user": email,
        "active_patterns": _active_patterns(ctx.flags),
        "peer_users": len(peer_profiles),
    }
    return _tool_result("get_user_profile", "user", figures, labels)


# Governance caps get_governance_config reports, with the figure kind each is shown as.
AGENT_CAP_FIELDS = (
    ("max_output_tokens_per_model_call", "tokens"),
    ("max_tool_calls", "count:calls"),
    ("max_iterations", "count:iterations"),
    ("max_tokens_per_agent_call", "tokens"),
)
USER_DEFAULT_CAP_FIELDS = (
    "daily_max_tokens_per_user",
    "monthly_max_tokens_per_user",
    "max_tokens_per_minute_per_user",
)  # all token caps


def _add_cap_usage_figures(
    figures: Figures, usage_vs_cap: list[dict[str, Any]], cap: int
) -> list[dict[str, Any]]:
    """Usage and share of the cap for the first CAP_USERS_SHOWN users (analytics ranks them by usage);
    returns their {rank, user} labels."""
    users = []
    for rank, usage in enumerate(usage_vs_cap[:CAP_USERS_SHOWN], start=1):
        email = safe_email(usage.get("user"))
        used = int(usage["used"])
        figures.add(_figure_key("user", rank, "used"), used, "tokens", f"{email} usage", "user")
        figures.add(
            _figure_key("user", rank, "pct_of_cap"),
            _percent(used, cap),
            "pct",
            f"{email} share of cap",
            "user",
        )
        users.append({"rank": rank, "user": email})
    return users


def _add_violation_figures(figures: Figures, violations: list[dict[str, Any]]) -> None:
    """A hit count for each in-loop cap that fired; caps that never fired are left out."""
    for violation in violations:
        count = int(violation.get("count", 0) or 0)
        if count:
            config_name = violation["config"]
            figures.add(
                _figure_key("violations", config_name),
                count,
                "count:times",
                f"{config_name} cap hits",
                "config",
                headline=True,
            )


def get_plan_vs_actual(ctx: AdvisorContext, window: str = "7d", user: str = "all") -> dict[str, Any]:
    """How usage compares with the window-matched UsagePlans cap, and which caps fired (R4).

    Args:
        window: "24h", "7d" or "30d"; the cap is the one matching it (daily, weekly or monthly).
        user: "all", an email or a name.

    Figures: `window_cap`; `users_near_quota` (at or above NEAR_QUOTA_SHARE of the cap); `user.<rank>.used` and
    `user.<rank>.pct_of_cap` for the CAP_USERS_SHOWN heaviest users; `violations.<config>` for each in-loop cap
    that fired; `total_violations`, `quota_blocks` (403) and `throttles` (429).
    Labels: window, user, cap_period and users ({rank, user}). Always available.
    Raises AdvisorInputError for an unknown window or user.
    """
    scope = _resolve_scope(ctx, window, user)
    analytics = ctx.analytics(scope.email, scope.window)
    cap = int(analytics.get("window_cap", 0) or 0)
    kpis = analytics["kpis"]
    figures = Figures()
    figures.add(
        "window_cap",
        cap,
        "tokens",
        f"{analytics.get('cap_period', '')} cap per user".strip(),
        "config",
        headline=True,
    )
    figures.add(
        "users_near_quota",
        int(kpis["users_near_quota"]),
        "count:users",
        f"Users at or above {int(NEAR_QUOTA_SHARE * 100)}% of the cap",
        "user",
        headline=True,
    )
    users = _add_cap_usage_figures(figures, analytics.get("usage_vs_cap", []), cap)
    _add_violation_figures(figures, analytics.get("violations", []))
    figures.add(
        "total_violations", int(kpis["total_violations"]), "count:times", "In-loop cap violations", "config"
    )
    figures.add("quota_blocks", int(kpis["quota_blocks"]), "count:times", "Quota blocks (403)", "user")
    figures.add("throttles", int(kpis["throttles"]), "count:times", "Throttles (429)", "user")
    labels = {
        "window": scope.window,
        "user": scope.email,
        "cap_period": analytics.get("cap_period"),
        "users": users,
    }
    return _tool_result(
        "get_plan_vs_actual",
        "user",
        figures,
        labels,
        f"near quota = at or above {int(NEAR_QUOTA_SHARE * 100)}% of the window-matched cap",
    )


def _cap_setting(settings: dict[str, Any], field: str) -> int:
    """A configured cap as an int; missing or empty reads 0 (no cap)."""
    return int(settings.get(field, 0) or 0)


def get_governance_config(ctx: AdvisorContext) -> dict[str, Any]:
    """The governance caps currently configured (0 = no cap).

    Figures (all at level "config"): `agent.<agent>.<field>` for each AGENT_CAP_FIELDS cap of every configured
    agent, in agent-name order; `session_max_tokens`; `user_default.<field>` for each USER_DEFAULT_CAP_FIELDS
    per-user default. No labels. Always available.
    """
    config = ctx.governance_config
    figures = Figures()
    for configured_name, agent_caps in sorted((config.get("agents") or {}).items()):
        agent_name = safe_agent(configured_name)
        for field, kind in AGENT_CAP_FIELDS:
            figures.add(
                _agent_key(agent_name, field),
                _cap_setting(agent_caps, field),
                kind,
                f"{agent_name} {field}",
                "config",
            )
    figures.add(
        "session_max_tokens",
        _cap_setting(config, "session_max_tokens"),
        "tokens",
        "session_max_tokens",
        "config",
    )
    user_default = config.get("user_default") or {}
    for field in USER_DEFAULT_CAP_FIELDS:
        figures.add(
            _figure_key("user_default", field), _cap_setting(user_default, field), "tokens", field, "config"
        )
    return _tool_result("get_governance_config", "config", figures)


def _dedupe_turns(turn_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ledger's turns in timestamp order, one per timestamp. EventBridge can redeliver a turn event and
    the duplicate carries the same ts, so the first entry for a ts wins."""
    first_by_ts: dict[str, dict[str, Any]] = {}
    for turn in turn_log:
        first_by_ts.setdefault(str(turn.get("ts", "")), turn)
    return [first_by_ts[ts] for ts in sorted(first_by_ts)]


def _spike_turn(tokens_per_turn: list[int]) -> int:
    """The 1-based number of the turn with the most tokens (the earliest one on a tie)."""
    return tokens_per_turn.index(max(tokens_per_turn)) + 1


def _model_calls_by_agent(turns: list[dict[str, Any]]) -> dict[str, int]:
    """Model calls per (sanitised) agent name, summed over the turns."""
    calls: dict[str, int] = defaultdict(int)
    for turn in turns:
        for agent_name, agent_run in (turn.get("agents") or {}).items():
            calls[safe_agent(agent_name)] += int(agent_run.get("model_calls", 0) or 0)
    return dict(calls)


def _governance_outcomes(turns: list[dict[str, Any]]) -> dict[str, int]:
    """How many turns ended with each governance outcome (reason); turns without one are left out."""
    turns_by_reason: dict[str, int] = defaultdict(int)
    for turn in turns:
        reason = str((turn.get("governance") or {}).get("reason") or "")
        if reason:
            turns_by_reason[reason] += 1
    return dict(turns_by_reason)


def get_turn_curve(ctx: AdvisorContext, session_id: str = "") -> dict[str, Any]:
    """How tokens and cost move turn over turn in one session, from its TURNS#<session_id> ledger (R10).

    Args:
        session_id: the session's id (letters, digits, '-' or '_'); anything else raises AdvisorInputError.

    Figures: `turn.<n>.tokens` and `turn.<n>.cost` per turn (1-based, timestamp order); `turns`;
    `spike.tokens` and `spike.x_over_median` for the largest turn; `total.cost`; `model_calls.<agent>` per
    agent; `governance.<reason>` for each governance outcome the turns hit.
    Labels: session_id and spike_turn. Unavailable when the session has no per-turn ledger (turns are recorded
    only after the R10 metering enrichment is deployed).
    """
    session_id = str(session_id or "")
    if not _SESSION_RE.match(session_id):
        raise AdvisorInputError("session_id must contain only letters, digits, '-' or '_'")
    row = ctx.turn_row(session_id)
    if not row or not row.get("turn_log"):
        return _unavailable_result(
            "get_turn_curve",
            "session",
            "no per-turn data for this session; turns are recorded only after the Advisor "
            "metering enrichment (R10) is deployed",
        )
    turns = _dedupe_turns(row["turn_log"])
    tokens_per_turn = [_tokens(turn) for turn in turns]
    figures = Figures()
    for number, (turn, tokens) in enumerate(zip(turns, tokens_per_turn), start=1):
        figures.add(
            _figure_key("turn", number, "tokens"), tokens, "tokens", f"Turn {number} tokens", "session"
        )
        figures.add(_figure_key("turn", number, "cost"), _cost(turn), "usd", f"Turn {number} cost", "session")
    spike_turn = _spike_turn(tokens_per_turn)
    spike_tokens = tokens_per_turn[spike_turn - 1]
    figures.add("turns", len(turns), "count:turns", "Turns in session", "session", headline=True)
    figures.add(
        "spike.tokens", spike_tokens, "tokens", f"Spike turn {spike_turn} tokens", "session", headline=True
    )
    figures.add(
        "spike.x_over_median",
        _ratio(spike_tokens, median(tokens_per_turn)),
        "x",
        f"Spike turn {spike_turn} vs median turn",
        "session",
        headline=True,
    )
    figures.add(
        "total.cost", sum(_cost(turn) for turn in turns), "usd", "Session cost", "session", headline=True
    )
    for agent_name, model_calls in sorted(_model_calls_by_agent(turns).items()):
        figures.add(
            _figure_key("model_calls", agent_name),
            model_calls,
            "count:calls",
            f"{agent_name} model calls (delegation/loop proxy)",
            "agent",
        )
    for reason, turn_count in sorted(_governance_outcomes(turns).items()):
        figures.add(
            _figure_key("governance", reason),
            turn_count,
            "count:turns",
            f"Turns with governance outcome {reason}",
            "session",
        )
    return _tool_result(
        "get_turn_curve",
        "session",
        figures,
        {"session_id": session_id, "spike_turn": spike_turn},
        "model_calls counts each agent run; tool-call counts are not recorded yet",
    )


TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_cost_summary": get_cost_summary,
    "get_period_comparison": get_period_comparison,
    "get_cache_health": get_cache_health,
    "get_outliers": get_outliers,
    "get_user_profile": get_user_profile,
    "get_plan_vs_actual": get_plan_vs_actual,
    "get_governance_config": get_governance_config,
    "get_turn_curve": get_turn_curve,
}
