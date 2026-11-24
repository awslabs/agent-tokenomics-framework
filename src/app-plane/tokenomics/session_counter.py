"""App-side per-session token counter for the soft per-session cap.

The per-session cap (`session_max_tokens`) spans multiple turns, and each turn
may run in a fresh AgentCore microVM, so the running total must live in an
external store. It uses the `Tokenomics-EnforcementCounter` table (shared with
the per-user quota counter) via a `SESSION#<id>` row.

Policy: **fail-OPEN**. The session cap is a UX guard, not a money limit -- if the
table isn't configured or a read/write fails, the cap is simply skipped
(baseline) rather than breaking a customer turn. The shared mechanics live in
`_counter_store`; this module owns only the `SESSION#` key shape + the fail-open
read policy.
"""

from __future__ import annotations

import config
from tokenomics import _counter_store


def _key(session_id: str) -> str:
    return f"SESSION#{session_id}"


def _ttl_seconds() -> int:
    return int(config.ENFORCEMENT_COUNTER_TTL_DAYS) * 86400


def session_tokens(session_id: str) -> int:
    """Tokens accumulated so far this session (0 if unknown / not configured).

    Fail-open: a store error returns 0 so the pre-turn cap check is skipped.
    """
    if not config.ENFORCEMENT_COUNTER_TABLE or not session_id:
        return 0
    return _counter_store.read_tokens(_key(session_id), fail_closed=False)


def add_session_tokens(session_id: str, tokens: int) -> None:
    """Atomically add this turn's tokens to the session counter (fire-and-forget)."""
    if not config.ENFORCEMENT_COUNTER_TABLE or not session_id or tokens <= 0:
        return
    _counter_store.add_tokens(_key(session_id), tokens, _ttl_seconds())
