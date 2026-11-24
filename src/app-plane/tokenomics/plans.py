"""Live usage-plans reader for the agent (DynamoDB-backed).

Mirrors `tokenomics.flags`: reads the shared `Tokenomics-UsagePlans` table that
the control plane writes (control writes, app reads), reassembles the flat
one-item-per-plan rows into a `GovernanceConfig`, and caches it in-process for a
short TTL so reads are near-zero cost on the hot path.

A benchmark can pin an exact config via `set_override(...)` (the entrypoint does
this from the request payload) so a whole run uses one known governance config
regardless of what the live table says -- exactly like `flags.set_override`.

Fail closed (threat model M13, T7): if the table is unreachable, throttled, empty
or malformed, the agent keeps the last config it read successfully; if it has
never read one, it uses `FAIL_SAFE` (the shipped default caps). A config-store
hiccup must never silently turn every money cap off. Only an unset
USAGE_PLANS_TABLE (local dev / unit tests) means "no caps".
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache

import boto3

import config
from shared import dynamo
from shared.usage_plans_schema import GovernanceConfig

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: GovernanceConfig | None = None
_cached_at: float = 0.0
_override: GovernanceConfig | None = None
_last_good: GovernanceConfig | None = None

# Conservative caps used when no live config has ever been read (mirrors
# seed/data/usage_plans.json). Zero still means "no cap" for a field.
FAIL_SAFE = GovernanceConfig.from_dict(
    {
        "agents": {
            "orchestrator": {
                "max_output_tokens_per_model_call": 4000,
                "max_tool_calls": 6,
                "max_iterations": 8,
                "max_tokens_per_agent_call": 60000,
            },
            "knowledge": {
                "max_output_tokens_per_model_call": 2000,
                "max_tool_calls": 4,
                "max_iterations": 5,
                "max_tokens_per_agent_call": 40000,
            },
            "order": {
                "max_output_tokens_per_model_call": 2000,
                "max_tool_calls": 5,
                "max_iterations": 5,
                "max_tokens_per_agent_call": 50000,
            },
        },
        "session_max_tokens": 150000,
        "user_default": {
            "daily_max_tokens_per_user": 200000,
            "monthly_max_tokens_per_user": 4000000,
            "max_tokens_per_minute_per_user": 40000,
        },
        "user_overrides": {},
    }
)


@lru_cache(maxsize=1)
def _plans_table():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.USAGE_PLANS_TABLE)


def _read_table() -> GovernanceConfig:
    """Scan the plans table and reassemble into a GovernanceConfig.

    Fails closed: on any read/parse failure, or an empty table, returns the last
    good config, else FAIL_SAFE -- never "no caps".
    """
    global _last_good
    if not config.USAGE_PLANS_TABLE:
        return GovernanceConfig.empty()
    try:
        items = _plans_table().scan().get("Items", [])
        if not items:
            raise ValueError("usage plans table is empty")
        cfg = GovernanceConfig.from_items([_plain(i) for i in items])
    except Exception as exc:  # noqa: BLE001 -- fail closed, log for ops
        fallback = "last good config" if _last_good is not None else "fail-safe caps"
        logger.warning("[plans] read failed, using %s (%s: %s)", fallback, type(exc).__name__, exc)
        return _last_good if _last_good is not None else FAIL_SAFE
    _last_good = cfg
    return cfg


def _plain(item: dict) -> dict:
    """Convert DynamoDB Decimals to plain int/float so the schema sees numbers.

    Thin wrapper over the shared float<->Decimal boundary (shared/dynamo).
    """
    return dynamo.to_plain(item)


def current() -> GovernanceConfig:
    """Return the active usage plans (override wins, else short-TTL table read)."""
    global _cached, _cached_at
    if _override is not None:
        return _override
    now = time.monotonic()
    with _lock:
        if _cached is None or (now - _cached_at) > config.CONFIG_CACHE_TTL_SECONDS:
            _cached = _read_table()
            _cached_at = now
        return _cached


def set_override(cfg: GovernanceConfig | None) -> None:
    """Pin an exact usage-plans config for the process (used for benchmark runs).

    Pass None to clear the override and return to reading the live table.
    """
    global _override
    _override = cfg


def invalidate() -> None:
    """Drop the cached read so the next `current()` re-reads immediately."""
    global _cached, _cached_at
    with _lock:
        _cached = None
        _cached_at = 0.0
