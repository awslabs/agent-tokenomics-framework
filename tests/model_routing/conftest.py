"""Unit-test fixtures for the model-routing lever (offline, no AWS).

Mirrors the app-plane container's PYTHONPATH (/app:/app/app-plane) so the agent
modules import exactly as they do on AgentCore, and sets the env vars that
app-plane `config` requires at import time (dummy values; nothing is called).

Run (needs the app-plane deps, e.g. the app-plane venv):

    src/app-plane/.venv/bin/python -m pytest tests/model_routing -v
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(_ROOT, "src", "app-plane"), os.path.join(_ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

for var in ("ORDERS_TABLE", "FAQ_TABLE", "FLAGS_TABLE"):
    os.environ.setdefault(var, f"test-{var.lower()}")
os.environ.setdefault("AWS_REGION", "us-west-2")
