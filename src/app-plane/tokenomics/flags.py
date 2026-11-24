"""Live feature-flag reader for the agent (DynamoDB-backed).

Reads the shared Flags table that the control plane writes. The table stores one
item per feature: { "feature": <name>, "enabled": bool, "params": {...} }. This
reassembles those items into a FlagConfig.

The config is cached in-process for a short TTL (config.FLAGS_CACHE_TTL_SECONDS)
so flag reads are near-zero cost on the hot path while still picking up control-
plane changes within a couple of seconds. A benchmark can pin an exact config
via `set_override(...)` (the entrypoint does this from the request payload) so a
whole run uses one known flag_config regardless of what the live table says.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache
from typing import Any

import boto3

import config
from shared.flags_schema import ALL_FEATURES, FlagConfig, default_flag_config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: FlagConfig | None = None
_cached_at: float = 0.0
_override: FlagConfig | None = None


@lru_cache(maxsize=1)
def _flags_table():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.FLAGS_TABLE)


def _read_table() -> dict[str, Any]:
    """Read every feature item and reassemble the FlagConfig dict shape.

    Falls back to the all-off baseline if the table is empty or unreachable, so
    a flag-store hiccup degrades to baseline behavior rather than crashing.
    """
    try:
        resp = _flags_table().scan()
    except Exception as exc:  # noqa: BLE001 -- degrade to baseline, but LOG first (degradation policy)
        logger.warning("[flags] read failed, running all-off baseline (%s: %s)", type(exc).__name__, exc)
        return default_flag_config()

    by_feature = {item["feature"]: item for item in resp.get("Items", [])}
    out: dict[str, Any] = {}
    for feature in ALL_FEATURES:
        item = by_feature.get(feature, {})
        out[feature] = {
            "enabled": bool(item.get("enabled", False)),
            "params": dict(item.get("params", {}) or {}),
        }
    return out or default_flag_config()


def current() -> FlagConfig:
    """Return the active flag config (override wins, else short-TTL table read)."""
    global _cached, _cached_at
    if _override is not None:
        return _override
    now = time.monotonic()
    with _lock:
        if _cached is None or (now - _cached_at) > config.FLAGS_CACHE_TTL_SECONDS:
            _cached = FlagConfig.from_dict(_read_table())
            _cached_at = now
        return _cached


def set_override(cfg: FlagConfig | None) -> None:
    """Pin an exact flag config for the process (used for benchmark runs).

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
