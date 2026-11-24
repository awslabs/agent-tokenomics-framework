"""Figures -- every number a tool returns, keyed so the agent can cite it (see __init__.py)."""

from __future__ import annotations

import re
from typing import Any

from .formatting import _format_figure

# ---- figures: every number a tool returns, keyed so the agent can cite it ---------

FIGURE_KEY_MAX_LEN = 80
FIGURE_DECIMALS = 6  # a figure's float value is stored rounded to this many decimals


def _figure_key(*parts: Any) -> str:
    """A figure's citable key: the parts joined by '.', lower-cased, any other character as '_'."""
    raw = ".".join(str(part) for part in parts)
    return re.sub(r"[^a-z0-9_.-]+", "_", raw.lower()).strip("_.")[:FIGURE_KEY_MAX_LEN]


# The key builders below are shared by the tools (which write the figures) and the signal
# rules (which read them), so both halves always agree on a key.
def _agent_key(agent_name: str, *metric: Any) -> str:
    return _figure_key("agent", agent_name, *metric)


def _model_key(model_id: str, *metric: Any) -> str:
    return _figure_key("model", model_id, *metric)


class Figures:
    """Collects the figures one tool call exposes: key -> {value, display, signal, level[, headline]}.

    `signal` is the figure's plain-text name, `level` what it is about (fleet, user, agent, model,
    session or config), and `headline` marks the figures shown when narration fails grounding."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(
        self, key: str, value: float | None, kind: str, signal: str, level: str, headline: bool = False
    ) -> None:
        """Add a figure; a None value (not computable for this data) is skipped."""
        if value is None:
            return
        stored = round(float(value), FIGURE_DECIMALS) if isinstance(value, float) else value
        figure = {"value": stored, "display": _format_figure(kind, value), "signal": signal, "level": level}
        if headline:
            figure["headline"] = True
        self.items[key] = figure
