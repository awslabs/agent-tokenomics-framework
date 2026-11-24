"""Signal rules: what a tool's figures show, matched to catalog patterns.

Deterministic detectors, one per tool (keyed in SIGNAL_RULES). Each takes the tool's
`(figures, labels)` dicts and returns number-free signal descriptions with the figure keys
behind them -- it reads figures, never the tools, so this half has no dependency back on
tools.py. See service/__init__.py for how SIGNAL_RULES is consumed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .context import CACHE_DROP_POINTS
from .figures import _agent_key, _figure_key, _model_key

# ---- signals: what the figures show, matched to catalog patterns ---------------
# Deterministic, so the same data always points at the same patterns. Thresholds follow the
# catalog's threshold lines where it has one: TKN-02 one model > ~70% of spend; TKN-03/18 hit
# rate < ~50% or read:write < ~3:1; TKN-01 input > ~90% of tokens; TKN-12/17 ~10x the median.
# The text is number-free; the figures are referenced by key (the agent cites them).
# The data tools attach each pattern's catalog fields (advisor/data_tools.py).

MODEL_DOMINANT_SHARE_PCT = 70.0  # TKN-02: one model carries more than 70% of the spend
AGENT_DOMINANT_SHARE_PCT = 50.0  # TKN-17: one agent carries at least half of the spend
HIGH_OUTPUT_SHARE_PCT = 30.0  # TKN-07/15: output is more than 30% of an agent's tokens
TOP_USER_SIGNAL_X = 3.0  # TKN-17: the top user spends at least 3x the median user
TOP_USER_OUTLIER_X = 10.0  # TKN-12: the top user spends at least 10x the median user
PERIOD_RISE_PCT = 25.0  # TKN-19/17: total cost up at least 25% on the pro-rated previous period
HEALTHY_CACHE_HIT_PCT = 50.0  # TKN-03/18: a cache hit rate below 50% is unhealthy
HEALTHY_READ_WRITE = 3.0  # TKN-03/18: a cache read:write below 3:1 is unhealthy
INPUT_DOMINANT_PCT = 90.0  # TKN-01/04: input is more than 90% of the tokens
PROFILE_SIGNAL_X = 2.0  # TKN-17, TKN-01/05: a user's sessions cost / run at least 2x the median user's
TURN_GROWTH_X = 3  # TKN-01/11: the last turn uses at least 3x the first turn's tokens (int: exact)
TURN_SPIKE_X = 3.0  # TKN-19: one turn uses at least 3x the session's median turn
VIOLATIONS_CITED = 3  # the in-loop caps signal cites at most this many violation figures


def _signal(
    text: str, level: str, patterns: tuple[str, ...], figures: dict[str, Any], *keys: str
) -> dict[str, Any]:
    """One signal: its number-free text, level, catalog patterns and the keys of the figures behind
    it (a key the tool did not produce is left out)."""
    cited = [key for key in keys if key in figures]
    return {"signal": text, "level": level, "patterns": list(patterns), "figures": cited}


def _figure_value(figures: dict[str, Any], key: str) -> float | None:
    """The figure's value (None when the tool did not produce it)."""
    return (figures.get(key) or {}).get("value")


def _figure_value_or_zero(figures: dict[str, Any], key: str) -> float:
    """The figure's value, a missing figure counting as 0 (for "is it above the threshold" checks)."""
    return _figure_value(figures, key) or 0


def _agent_spend_shares(figures: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, float]:
    """agent_name -> its share of spend (%), in label order, for the agents that have the figure."""
    shares: dict[str, float] = {}
    for agent in agents:
        agent_name = agent["agent"]
        share = _figure_value(figures, _agent_key(agent_name, "share_pct"))
        if share is not None:
            shares[agent_name] = share
    return shares


def _cost_summary_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_cost_summary: a model carrying most of the spend while a cheaper one is in use
    (TKN-02), an agent carrying most of the spend (TKN-17), an agent with a high output share
    (TKN-07, TKN-15), and a top user far above the median user (TKN-17, plus TKN-12 from
    TOP_USER_OUTLIER_X times the median)."""
    signals = []
    for model in labels.get("models") or []:
        share_key = _model_key(model["model_id"], "share_pct")
        multiple_key = _model_key(model["model_id"], "cost_per_1k_x_vs_cheapest")
        dominant = _figure_value_or_zero(figures, share_key) > MODEL_DOMINANT_SHARE_PCT
        # Over 1x the cheapest model's cost per 1k tokens: a cheaper model is already in use.
        cheaper_model_in_use = _figure_value_or_zero(figures, multiple_key) > 1
        if dominant and cheaper_model_in_use:
            signals.append(
                _signal(
                    f"{model['model']} carries most of the spend while a cheaper model is already in use",
                    "model",
                    ("TKN-02",),
                    figures,
                    share_key,
                    multiple_key,
                )
            )

    agent_shares = _agent_spend_shares(figures, labels.get("agents") or [])
    if agent_shares:
        top_agent = max(agent_shares, key=lambda agent_name: agent_shares[agent_name])
        if agent_shares[top_agent] >= AGENT_DOMINANT_SHARE_PCT:
            signals.append(
                _signal(
                    f"the {top_agent} agent carries most of the spend",
                    "agent",
                    ("TKN-17",),
                    figures,
                    _agent_key(top_agent, "share_pct"),
                    _agent_key(top_agent, "cost"),
                )
            )
    for agent_name in agent_shares:
        output_share_key = _agent_key(agent_name, "output_share_pct")
        if _figure_value_or_zero(figures, output_share_key) > HIGH_OUTPUT_SHARE_PCT:
            signals.append(
                _signal(
                    f"the {agent_name} agent's output share of tokens is high",
                    "agent",
                    ("TKN-07", "TKN-15"),
                    figures,
                    output_share_key,
                )
            )

    top_user_multiple_key = _figure_key("top_user", 1, "x_over_median")
    top_user_multiple = _figure_value_or_zero(figures, top_user_multiple_key)
    if top_user_multiple >= TOP_USER_SIGNAL_X:
        patterns = ("TKN-17", "TKN-12") if top_user_multiple >= TOP_USER_OUTLIER_X else ("TKN-17",)
        signals.append(
            _signal(
                "the top user spends far more than the median user",
                "user",
                patterns,
                figures,
                _figure_key("top_user", 1, "cost"),
                top_user_multiple_key,
                "users.median_cost",
            )
        )
    return signals


def _period_comparison_signals(
    figures: dict[str, Any], total_change_vs_prorated_pct: float | None
) -> list[dict[str, Any]]:
    """Signals for get_period_comparison, which calls this itself (the pro-rated change is not a
    figure): total cost rising sharply (TKN-19, TKN-17) and the cache hit rate dropping (TKN-18, TKN-03).

    `total_change_vs_prorated_pct` is the total's change % vs the pro-rated previous period (None when
    the previous period had no spend)."""
    signals = []
    if (total_change_vs_prorated_pct or 0) >= PERIOD_RISE_PCT:
        change_pct_key = _figure_key("total", "delta_pct")
        signals.append(
            _signal(
                "total cost rose sharply versus the previous period",
                figures[change_pct_key]["level"],
                ("TKN-19", "TKN-17"),
                figures,
                change_pct_key,
                _figure_key("total", "current"),
                _figure_key("total", "previous"),
            )
        )
    current_hit_key = _figure_key("cache.hit_pct", "current")
    previous_hit_key = _figure_key("cache.hit_pct", "previous")
    current_hit_pct = _figure_value(figures, current_hit_key)
    previous_hit_pct = _figure_value(figures, previous_hit_key)
    if (
        current_hit_pct is not None
        and previous_hit_pct is not None
        and current_hit_pct < previous_hit_pct - CACHE_DROP_POINTS
    ):
        signals.append(
            _signal(
                "the cache hit rate dropped versus the previous period",
                figures[current_hit_key]["level"],
                ("TKN-18", "TKN-03"),
                figures,
                current_hit_key,
                previous_hit_key,
            )
        )
    return signals


def _cache_health_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_cache_health, at the tool's level (fleet or one agent): the hit rate or
    read:write below the healthy level (TKN-03, TKN-18), a drop from a specific day (TKN-18), input
    dominating the token mix (TKN-01, TKN-04) and the prompt_cache flag being off (TKN-03)."""
    signals = []
    level = (figures.get("hit_pct") or {}).get("level", "fleet")
    hit_pct = _figure_value(figures, "hit_pct")
    read_write = _figure_value(figures, "read_write")
    low_hit_rate = hit_pct is not None and hit_pct < HEALTHY_CACHE_HIT_PCT
    low_read_write = read_write is not None and read_write < HEALTHY_READ_WRITE
    if low_hit_rate or low_read_write:
        signals.append(
            _signal(
                "the cache hit rate or read:write is below the healthy level",
                level,
                ("TKN-03", "TKN-18"),
                figures,
                "hit_pct",
                "read_write",
            )
        )
    first_drop_day = labels.get("first_drop_day")
    if first_drop_day:
        signals.append(
            _signal(
                "the cache hit rate dropped from a specific day",
                level,
                ("TKN-18",),
                figures,
                "hit_pct_previous",
                _figure_key("day", first_drop_day, "hit_pct"),
            )
        )
    if _figure_value_or_zero(figures, "input_share_pct") > INPUT_DOMINANT_PCT:
        signals.append(
            _signal("input dominates the token mix", level, ("TKN-01", "TKN-04"), figures, "input_share_pct")
        )
    if not labels.get("prompt_cache_enabled"):
        signals.append(_signal("the prompt_cache feature flag is off", level, ("TKN-03",), figures))
    return signals


def _outliers_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_outliers, one per kind it flagged, citing the first one flagged: a user-day
    (TKN-17, TKN-12), an agent's spike day (TKN-19, TKN-17) and a session's tokens per turn
    (TKN-19, TKN-11)."""
    signals = []
    if labels.get("users"):
        signals.append(
            _signal(
                "a user's daily spend is far above the peer median",
                "user",
                ("TKN-17", "TKN-12"),
                figures,
                _figure_key("users", 1, "cost"),
                _figure_key("users", 1, "x_over_median"),
            )
        )
    if labels.get("agents"):
        signals.append(
            _signal(
                "an agent had a spike day versus its own median day",
                "agent",
                ("TKN-19", "TKN-17"),
                figures,
                _figure_key("agents", 1, "cost"),
                _figure_key("agents", 1, "x_over_median"),
            )
        )
    if labels.get("sessions"):
        signals.append(
            _signal(
                "a session's tokens per turn are far above the peer average",
                "session",
                ("TKN-19", "TKN-11"),
                figures,
                _figure_key("sessions", 1, "tokens_per_turn"),
                _figure_key("sessions", 1, "x_over_avg"),
            )
        )
    return signals


def _user_profile_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_user_profile: the user's sessions costing (TKN-17) or running (TKN-01, TKN-05)
    PROFILE_SIGNAL_X times the median user's or more, and their cache hit rate below the healthy
    level (TKN-03)."""
    signals = []
    cost_multiple_key = _figure_key("x_vs_median", "cost_per_session")
    if _figure_value_or_zero(figures, cost_multiple_key) >= PROFILE_SIGNAL_X:
        signals.append(
            _signal(
                "this user's sessions cost much more than the median user's",
                "user",
                ("TKN-17",),
                figures,
                _figure_key("user", "cost_per_session"),
                cost_multiple_key,
            )
        )
    length_multiple_key = _figure_key("x_vs_median", "turns_per_session")
    if _figure_value_or_zero(figures, length_multiple_key) >= PROFILE_SIGNAL_X:
        signals.append(
            _signal(
                "this user's sessions run much longer than the median user's",
                "user",
                ("TKN-01", "TKN-05"),
                figures,
                _figure_key("user", "turns_per_session"),
                length_multiple_key,
            )
        )
    hit_pct_key = _figure_key("user", "cache_hit_pct")
    hit_pct = _figure_value(figures, hit_pct_key)
    if hit_pct is not None and hit_pct < HEALTHY_CACHE_HIT_PCT:
        signals.append(
            _signal(
                "this user's cache hit rate is below the healthy level",
                "user",
                ("TKN-03",),
                figures,
                hit_pct_key,
                _figure_key("median", "cache_hit_pct"),
            )
        )
    return signals


def _plan_vs_actual_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_plan_vs_actual: users at or near their quota (TKN-12), the top user over the cap
    with no quota block fired (TKN-12, TKN-13) and in-loop caps firing (TKN-08, TKN-09, TKN-10)."""
    signals = []
    top_user_pct_key = _figure_key("user", 1, "pct_of_cap")
    if _figure_value_or_zero(figures, "users_near_quota") > 0:
        signals.append(
            _signal(
                "users are at or near their quota",
                "user",
                ("TKN-12",),
                figures,
                "users_near_quota",
                top_user_pct_key,
                "window_cap",
            )
        )
    top_user_over_cap = _figure_value_or_zero(figures, top_user_pct_key) >= 100
    if top_user_over_cap and not _figure_value(figures, "quota_blocks"):
        signals.append(
            _signal(
                "a user is over the cap but no quota block has fired",
                "user",
                ("TKN-12", "TKN-13"),
                figures,
                top_user_pct_key,
                "quota_blocks",
            )
        )
    violation_keys = [key for key in figures if key.startswith("violations.")]
    if violation_keys:
        signals.append(
            _signal(
                "in-loop caps are firing",
                "config",
                ("TKN-08", "TKN-09", "TKN-10"),
                figures,
                *violation_keys[:VIOLATIONS_CITED],
            )
        )
    return signals


def _turn_curve_signals(figures: dict[str, Any], labels: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals for get_turn_curve: tokens growing from the first turn to the last (TKN-01, TKN-11), one
    turn spiking far above the session's median turn (TKN-19) and turns hitting a governance limit
    (TKN-08, TKN-09)."""
    signals = []
    turn_count = int(_figure_value_or_zero(figures, "turns"))
    first_turn_key = _figure_key("turn", 1, "tokens")
    last_turn_key = _figure_key("turn", turn_count, "tokens")
    first_tokens = _figure_value(figures, first_turn_key)
    last_tokens = _figure_value(figures, last_turn_key)
    if turn_count > 1 and first_tokens and last_tokens and last_tokens >= TURN_GROWTH_X * first_tokens:
        signals.append(
            _signal(
                "tokens grow turn over turn in this session",
                "session",
                ("TKN-01", "TKN-11"),
                figures,
                first_turn_key,
                last_turn_key,
            )
        )
    if _figure_value_or_zero(figures, "spike.x_over_median") >= TURN_SPIKE_X:
        signals.append(
            _signal(
                "one turn spiked far above the session's median turn",
                "session",
                ("TKN-19",),
                figures,
                "spike.tokens",
                "spike.x_over_median",
            )
        )
    governance_keys = [key for key in figures if key.startswith("governance.")]
    if governance_keys:
        signals.append(
            _signal(
                "turns in this session hit a governance limit",
                "session",
                ("TKN-08", "TKN-09"),
                figures,
                *governance_keys,
            )
        )
    return signals


# get_period_comparison is not listed: it calls its rule itself, with the pro-rated total change.
SIGNAL_RULES: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = {
    "get_cost_summary": _cost_summary_signals,
    "get_cache_health": _cache_health_signals,
    "get_outliers": _outliers_signals,
    "get_user_profile": _user_profile_signals,
    "get_plan_vs_actual": _plan_vs_actual_signals,
    "get_turn_curve": _turn_curve_signals,
}
