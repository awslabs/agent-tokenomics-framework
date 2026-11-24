"""Advisor Service -- the deterministic data layer behind the Tokenomics Advisor.

Deterministic functions over the Metrics / UsagePlans / Flags stores, plus cached
Cognito email look-ups (via handlers.governance). No model calls, no narration.
Every number the Advisor may state comes from here, as a `figures` entry
`{value, display, signal, level}` the agent cites by reference (it never types a
number itself) -- see advisor/grounding.py.

Layout: context.py (named rules, the per-turn context, input resolution and the
row-reading building blocks) -> formatting.py (display formatting) -> figures.py
(the key builders shared by the tools and the signal rules) -> tools.py (the
eight tools, each preceded by its own helpers, then the signal rules and the
SIGNAL_RULES / TOOLS registries).

Live traffic only: benchmark rows (BENCH# / source=benchmark) are dropped on
load, so no tool can ever see them. Totals reuse `handlers.governance.analytics()`
so the Advisor reconciles exactly with the Analytics / Governance pages.

Each tool returns:
    {"tool", "available", "data_level", "figures": {key: {...}}, "labels": {...},
     "note"?, "reason"?, "error"?}
"""

from __future__ import annotations

from advisor import WINDOWS

from .context import (
    INVALID,
    AdvisorContext,
    AdvisorInputError,
    TurnContext,
    resolve_user,
    safe_email,
    safe_model,
    safe_session,
)
from .formatting import format_tokens, format_usd, model_label
from .tools import (
    TOOLS,
    get_cache_health,
    get_cost_summary,
    get_governance_config,
    get_outliers,
    get_period_comparison,
    get_plan_vs_actual,
    get_turn_curve,
    get_user_profile,
)

# The package's public API. Underscore names are internal to the package (shared between its
# modules); tests reach them through the owning submodule (e.g. service.tools._trend), which is
# also where a monkeypatch takes effect.
__all__ = [
    "INVALID",
    "TOOLS",
    "WINDOWS",
    "AdvisorContext",
    "AdvisorInputError",
    "TurnContext",
    "format_tokens",
    "format_usd",
    "get_cache_health",
    "get_cost_summary",
    "get_governance_config",
    "get_outliers",
    "get_period_comparison",
    "get_plan_vs_actual",
    "get_turn_curve",
    "get_user_profile",
    "model_label",
    "resolve_user",
    "safe_email",
    "safe_model",
    "safe_session",
]
