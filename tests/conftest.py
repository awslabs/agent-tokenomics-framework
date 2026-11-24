"""Shared fixtures for the unit tests in tests/ (no AWS)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATHS = (_ROOT / "src", _ROOT / "src" / "control-plane", _ROOT / "seed")
_TABLES = ("FLAGS_TABLE", "RUNS_TABLE", "USAGE_PLANS_TABLE", "METRICS_TABLE")
_FRESH = ("config", "lib", "handlers", "lambda_entry", "metering_consumer")
_FRESH_PREFIXES = ("lib.", "handlers.", "advisor")


@pytest.fixture
def cp_import(monkeypatch):
    """Import control-plane modules fresh, so each test reads its own env vars.

    Sets the table env vars `config` needs and puts src/, src/control-plane and seed/
    on sys.path. `cp_import(*names)` drops the cached control-plane modules, then
    imports `names` (one module, or a tuple for several). monkeypatch undoes it all.
    """
    for k in _TABLES:
        monkeypatch.setenv(k, "x")
    for p in _PATHS:
        monkeypatch.syspath_prepend(str(p))

    def load(*names):
        for name in list(sys.modules):
            if name in _FRESH or name.startswith(_FRESH_PREFIXES):
                monkeypatch.delitem(sys.modules, name, raising=False)
        mods = tuple(importlib.import_module(n) for n in names)
        return mods[0] if len(mods) == 1 else mods

    return load
