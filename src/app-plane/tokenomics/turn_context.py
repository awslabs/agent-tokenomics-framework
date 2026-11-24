"""Per-turn user context the tools read.

Tools are plain module functions, so the orchestrator runner stashes the current
turn's user identity + profile here (contextvar, so it's correct even if turns
run concurrently in the same process). `lookup_customer` reads the profile from
here (forwarded from the verified JWT claims) instead of a Customers table;
`get_orders` reads `user_id` to query the Orders table by the Cognito `sub`.
"""

from __future__ import annotations

import contextvars
from typing import Any

# Default is None (not {}): a mutable default would be ONE dict shared across all
# contexts. `handle_turn` calls set_current() at the start of every turn, so the
# getters only need to tolerate the pre-first-set None.
_current: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("turn_context", default=None)


def set_current(user_id: str, profile: dict[str, Any]) -> None:
    _current.set({"user_id": user_id, "profile": profile or {}})


def user_id() -> str:
    return (_current.get() or {}).get("user_id", "")


def profile() -> dict[str, Any]:
    return (_current.get() or {}).get("profile", {})
