"""One-call logging setup for the runtime entrypoints.

Our modules log via `logging.getLogger(__name__)` (see the degradation policy in
`tokenomics/__init__.py`). But a logger only emits if the ROOT logger is
configured -- otherwise Python's last-resort handler drops everything below
WARNING, so INFO diagnostics never reach CloudWatch. Each entrypoint calls
`configure_logging()` once at import time to fix that.

Two runtimes, handled in one place:
  * AgentCore container (our agent images) -- no handler is pre-installed, so we
    add one via `basicConfig` with a format.
  * AWS Lambda (control-plane functions) -- the runtime installs a root handler
    before our code loads, so `basicConfig` would be a no-op; we set the level on
    the existing root logger (and its handlers) instead.

Level comes from `LOG_LEVEL` (default INFO), so ops can dial it up/down per
deployment without a code change.
"""

from __future__ import annotations

import logging
import os

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(default_level: str = "INFO") -> None:
    """Configure the root logger once, idempotently. Safe to call on every import."""
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if root.handlers:
        # A handler already exists (AWS Lambda installs one before our code runs).
        # basicConfig would no-op, so set the level on the root + its handlers.
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)
    else:
        # No handler yet (AgentCore container): install one with our format.
        logging.basicConfig(level=level, format=_LOG_FORMAT)
