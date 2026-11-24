"""LIVE enforcement tests — prove all 8 governance settings actually fire.

For each setting we set a deliberately tiny cap (so even a single short turn
crosses it), send ONE real chat turn as the shipped demo user `bob` through the
deployed agent, and assert the enforced outcome:

  per-user money caps  -> HTTP status (403 quota / 429 throttle)
  in-loop agent caps   -> HTTP 200 + a `governance` outcome in the body

The `config_guard` fixture snapshots the config before each test and restores it
after (even on failure), so a run leaves the dev's settings unchanged.

COST: each test sends one short chat turn (small Bedrock spend). The caps are
set to 1, so the quota/throttle turns are blocked BEFORE the model call (no
Bedrock cost); the agent-cap turns do run the model briefly.

Run:
    bash tests/governance/test-governance.sh enforcement
    src/app-plane/.venv/bin/python -m pytest tests/governance/test_enforcement.py -m live -v
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.live

# Config reads are cached in the agent with a short TTL (~2s). Wait a bit longer
# than that after a PUT so the next turn sees the new value.
_TTL_WAIT = 5

# Generous baseline: every cap effectively off (0 = no cap) EXCEPT the one a test
# sets. Agent caps use large values rather than 0 so a test can lower just one.
_BASE_AGENTS = {
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
}
_NO_QUOTA = {
    "daily_max_tokens_per_user": 0,
    "monthly_max_tokens_per_user": 0,
    "max_tokens_per_minute_per_user": 0,
}


def _base(**overrides):
    """A full editable config with everything generous; apply overrides on top."""
    import copy

    cfg = {
        "agents": copy.deepcopy(_BASE_AGENTS),
        "session_max_tokens": 150000,
        "user_default": dict(_NO_QUOTA),
    }
    cfg.update(overrides)
    return cfg


def _conv(tag: str) -> str:
    return f"livetest-{tag}-{uuid.uuid4().hex[:8]}"


def _apply(set_partial, cfg):
    set_partial(cfg)
    time.sleep(_TTL_WAIT)


def _governance(resp) -> dict:
    return (resp.json() or {}).get("governance") or {}


# ===========================================================================
# Per-user money caps  (quota hook -> HTTP status codes)
# ===========================================================================


def test_daily_quota_blocks_403(gov, config_guard):
    """#6 daily_max_tokens_per_user: over -> HTTP 403 BUDGET_EXCEEDED (daily)."""
    cfg = _base()
    cfg["user_default"]["daily_max_tokens_per_user"] = 1
    _apply(config_guard, cfg)

    r = gov.chat("bob", "Hi, what is your return policy?", _conv("daily"))
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("error") == "BUDGET_EXCEEDED"
    assert body.get("limit_hit") == "daily"


def test_monthly_quota_blocks_403(gov, config_guard):
    """#7 monthly_max_tokens_per_user: over -> HTTP 403 BUDGET_EXCEEDED (monthly)."""
    cfg = _base()
    cfg["user_default"]["monthly_max_tokens_per_user"] = 1
    _apply(config_guard, cfg)

    r = gov.chat("bob", "Hi, what is your return policy?", _conv("monthly"))
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("error") == "BUDGET_EXCEEDED"
    assert body.get("limit_hit") == "monthly"


def test_tpm_throttle_429(gov, config_guard):
    """#8 max_tokens_per_minute_per_user: over -> HTTP 429 with retryAfter."""
    cfg = _base()
    cfg["user_default"]["max_tokens_per_minute_per_user"] = 1
    _apply(config_guard, cfg)

    r = gov.chat("bob", "Hi, what is your return policy?", _conv("tpm"))
    assert r.status_code == 429, r.text
    body = r.json()
    assert body.get("retryAfter")  # present and non-zero


# ===========================================================================
# In-loop agent caps  (HTTP 200 + governance outcome in the body)
# ===========================================================================


def test_session_cap_blocks_next_turn(gov, config_guard):
    """#5 session_max_tokens: a turn after the session total exceeds the cap is
    blocked pre-emptively (reason=session_budget, empty usage)."""
    conv = _conv("session")
    # Turn 1 with a generous session cap: should answer and spend tokens.
    _apply(config_guard, _base())
    r1 = gov.chat("bob", "Hi, what is your return policy?", conv)
    assert r1.status_code == 200, r1.text
    assert not _governance(r1).get("stopped")

    # Lower the session cap below what turn 1 already spent, then re-ask in the
    # SAME conversation -> pre-turn session block.
    _apply(config_guard, _base(session_max_tokens=10))
    r2 = gov.chat("bob", "And how long do refunds take?", conv)
    assert r2.status_code == 200, r2.text
    g = _governance(r2)
    assert g.get("stopped") is True
    assert g.get("reason") == "session_budget"
    assert g.get("scope") == "session"
    assert g.get("config") == "session_max_tokens"


def test_output_token_cap_cuts_response(gov, config_guard):
    """#1 max_output_tokens_per_model_call: model output is cut at the cap."""
    cfg = _base()
    cfg["agents"]["orchestrator"]["max_output_tokens_per_model_call"] = 16
    _apply(config_guard, cfg)

    r = gov.chat(
        "bob",
        "Please explain in detail your full return, refund, and exchange policy with examples.",
        _conv("outcap"),
    )
    assert r.status_code == 200, r.text
    g = _governance(r)
    assert g.get("reason") == "limit_output_tokens"
    assert g.get("config") == "max_output_tokens_per_model_call"
    assert g.get("limited") is True


def test_tool_call_cap_limits_turn(gov, config_guard):
    """#2 max_tool_calls: agent still answers but skips tools past the cap."""
    cfg = _base()
    cfg["agents"]["orchestrator"]["max_tool_calls"] = 1
    _apply(config_guard, cfg)

    r = gov.chat(
        "bob",
        "Check my recent order status AND tell me your return policy AND whether I can change my shipping address.",
        _conv("toolcap"),
    )
    assert r.status_code == 200, r.text
    g = _governance(r)
    assert g.get("reason") == "tool_cap"
    assert g.get("config") == "max_tool_calls"
    assert g.get("limited") is True
    assert g.get("agent") == "orchestrator"


def test_iteration_cap_stops_loop(gov, config_guard):
    """#3 max_iterations: the agent loop stops early (reason=limit_turns)."""
    cfg = _base()
    cfg["agents"]["orchestrator"]["max_iterations"] = 1
    _apply(config_guard, cfg)

    r = gov.chat(
        "bob",
        "Look up the status of my most recent order and tell me when it will arrive.",
        _conv("iter"),
    )
    assert r.status_code == 200, r.text
    g = _governance(r)
    assert g.get("reason") == "limit_turns"
    assert g.get("config") == "max_iterations"
    assert g.get("stopped") is True


def test_agent_token_cap_stops_turn(gov, config_guard):
    """#4 max_tokens_per_agent_call: agent-call token total cap stops the turn."""
    cfg = _base()
    cfg["agents"]["orchestrator"]["max_tokens_per_agent_call"] = 50
    _apply(config_guard, cfg)

    r = gov.chat("bob", "Hi, what is your return policy?", _conv("agenttok"))
    assert r.status_code == 200, r.text
    g = _governance(r)
    assert g.get("reason") == "limit_total_tokens"
    assert g.get("config") == "max_tokens_per_agent_call"
    assert g.get("stopped") is True
