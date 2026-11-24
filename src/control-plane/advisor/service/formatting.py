"""Display formatting -- deterministic; the only place a number becomes text (see __init__.py)."""

from __future__ import annotations

import re
from collections.abc import Callable

# ---- display formatting (deterministic; the only place numbers become text) --

USD_WHOLE_DOLLARS_FROM = 100  # $1,234
USD_CENTS_FROM = 0.01  # $12.35; anything smaller keeps 4 decimals ($0.0042)
TOKENS_MILLION = 1_000_000  # 1.2M tokens
TOKENS_THOUSAND = 1_000  # 12.3k tokens
COUNT_KIND = "count:"  # "count:<plural unit>" -> "3 sessions" / "1 session"
DECIMAL_KIND = "dec:"  # "dec:<unit>" -> "2.5 turns"


def format_usd(value: float) -> str:
    value = float(value)
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= USD_WHOLE_DOLLARS_FROM:
        return f"{sign}${amount:,.0f}"
    if amount >= USD_CENTS_FROM:
        return f"{sign}${amount:,.2f}"
    return f"{sign}${amount:.4f}"


def format_tokens(count: float) -> str:
    count = float(count)
    if abs(count) >= TOKENS_MILLION:
        return f"{count / TOKENS_MILLION:.1f}M tokens"
    if abs(count) >= TOKENS_THOUSAND:
        return f"{count / TOKENS_THOUSAND:.1f}k tokens"
    return f"{round(count)} tokens"


def _format_count(unit: str, value: float) -> str:
    count = round(value)
    if count == 1 and unit.endswith("s"):
        unit = unit[:-1]  # "1 session", not "1 sessions"
    return f"{count:,} {unit}"


_FIXED_FORMATS: dict[str, Callable[[float], str]] = {
    "usd": format_usd,
    "tokens": format_tokens,
    "pct": lambda value: f"{float(value):.1f}%",
    "x": lambda value: f"{float(value):.1f}×",
    "ratio": lambda value: f"{float(value):.1f}:1",
}


def _format_figure(kind: str, value: float) -> str:
    """Display text for a figure: `kind` is a _FIXED_FORMATS key, "count:<units>" or "dec:<unit>"."""
    if kind in _FIXED_FORMATS:
        return _FIXED_FORMATS[kind](value)
    if kind.startswith(COUNT_KIND):
        return _format_count(kind[len(COUNT_KIND) :], value)
    if kind.startswith(DECIMAL_KIND) and len(kind) > len(DECIMAL_KIND):
        return f"{float(value):.1f} {kind[len(DECIMAL_KIND) :]}"
    raise ValueError(f"unknown figure kind {kind!r}")


_MODEL_NAME = re.compile(
    r"(claude|nova)-(?:3-)?(sonnet|haiku|opus|lite|pro|micro)(?:-(\d+))?(?:-(\d))?", re.IGNORECASE
)


def model_label(model_id: str) -> str:
    """Friendly name for a model id, e.g. 'Claude Sonnet 5.5' (falls back to the id)."""
    match = _MODEL_NAME.search(model_id or "")
    if not match:
        return model_id
    family, tier, major, minor = match.groups()
    # A one-digit major is a version (4, 4.5); a longer number is a release date, not a version.
    version = ""
    if major and len(major) == 1:
        version = f" {major}.{minor}" if minor else f" {major}"
    return f"{family.capitalize()} {tier.capitalize()}{version}"
