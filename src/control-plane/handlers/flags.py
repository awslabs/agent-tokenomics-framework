"""/flags handler -- read and update feature flags.

The agent reads the same flag store, so a PUT here changes agent behavior on the
next turn (no redeploy). V1 flags: prompt_cache, memory, context_management
(see shared/flags_schema.py).
"""

from __future__ import annotations

from typing import Any

from lib import store


def get_flags() -> dict[str, Any]:
    """GET /flags -> the current flag config."""
    return store.read_flags()


def put_flags(body: dict[str, Any]) -> dict[str, Any]:
    """PUT /flags -> replace the flag config, return the normalized result.

    Accepts either a full config ({"prompt_cache": {"enabled": true, ...}}) or a
    partial merge over the current config so the UI can send just what changed.
    """
    current = store.read_flags()
    merged = {**current, **(body or {})}
    return store.write_flags(merged)
