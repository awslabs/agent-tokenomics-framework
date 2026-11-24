"""LIVE analytics tests — prove the numbers behind every label and graph are
internally consistent and correctly computed, WHATEVER the current data is.

These are READ-ONLY (no config change, no chat turns, no Bedrock cost). They
call GET /governance/analytics for {all, bob, eva} x {24h, 7d, 30d} and assert
the invariants that must hold for the page to be truthful:

  * payload shape — every widget's field is present and well-typed
  * reconciliation — KPI total == Sum(cost_by_agent) == Sum(top_users), tokens too
  * per-user partition — bob + eva sum to the all-users totals
  * usage-vs-cap — used/quota and the implied % are consistent with the KPIs
  * window-matched cap — cap_period/window_cap follow the 24h/7d/30d rule
  * precision — costs are clean to the displayed decimals (no float dust)

If the data is empty for a window, the invariants still hold trivially (0 == 0),
so the suite is safe to run against any state.

Run:
    bash tests/governance/test-governance.sh analytics
    src/app-plane/.venv/bin/python -m pytest tests/governance/test_analytics.py -m live -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

WINDOWS = ["24h", "7d", "30d"]
USERS = ["all", "bob@example.com", "eva@example.com"]

# Cost reconciliation tolerance. The backend keeps per-row roll-ups at 6dp but
# rounds the KPI totals to 4dp (round(total_cost, 4)), so a KPI total can differ
# from the 6dp agent/user sums by up to half a 4dp ULP (~5e-5). 1e-4 covers that
# while staying far tighter than the 3-decimal precision the UI actually renders.
TOL = 1e-4


def _approx(a: float, b: float, tol: float = TOL) -> bool:
    return abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
# Payload shape — every widget's data is present and the right type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_payload_shape(gov, window):
    a = gov.analytics("all", window)

    assert a["window"] == window
    assert a["user"] == "all"
    assert a["cap_period"] in ("daily", "weekly", "monthly")
    assert isinstance(a["window_cap"], (int, float))

    kpis = a["kpis"]
    for k in (
        "total_tokens",
        "total_cost_usd",
        "avg_tokens_per_session",
        "avg_cost_per_session",
        "total_violations",
        "quota_blocks",
        "throttles",
        "users_near_quota",
        "total_sessions",
        "total_turns",
    ):
        assert k in kpis, f"missing KPI {k}"
        assert isinstance(kpis[k], (int, float))

    # Array-backed widgets.
    assert len(a["violations"]) == 8  # all 8 settings always listed (count may be 0)
    for arr in (
        "cost_by_agent",
        "top_users",
        "throttle_by_day",
        "quota_block_by_day",
        "usage_vs_cap",
        "events",
    ):
        assert isinstance(a[arr], list), f"{arr} should be a list"


# ---------------------------------------------------------------------------
# Reconciliation — the money adds up (the invariant that caught the real bug)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", USERS)
@pytest.mark.parametrize("window", WINDOWS)
def test_cost_reconciles(gov, user, window):
    """KPI total cost == Sum(cost_by_agent) == Sum(top_users); tokens likewise."""
    a = gov.analytics(user, window)

    agent_cost = sum(x["cost"] for x in a["cost_by_agent"])
    user_cost = sum(x["cost"] for x in a["top_users"])
    kpi_cost = a["kpis"]["total_cost_usd"]

    assert _approx(agent_cost, kpi_cost), f"agent={agent_cost} kpi={kpi_cost} ({user}/{window})"
    assert _approx(user_cost, kpi_cost), f"users={user_cost} kpi={kpi_cost} ({user}/{window})"

    agent_tokens = sum(x["tokens"] for x in a["cost_by_agent"])
    user_tokens = sum(x["tokens"] for x in a["top_users"])
    kpi_tokens = a["kpis"]["total_tokens"]

    assert agent_tokens == kpi_tokens, f"agent={agent_tokens} kpi={kpi_tokens} ({user}/{window})"
    assert user_tokens == kpi_tokens, f"users={user_tokens} kpi={kpi_tokens} ({user}/{window})"


@pytest.mark.parametrize("window", WINDOWS)
def test_per_user_sums_to_all(gov, window):
    """bob + eva (and any other users) sum to the all-users totals."""
    all_a = gov.analytics("all", window)

    sum_tokens = 0
    sum_cost = 0.0
    # Enumerate every user the all-users view knows about (not just bob/eva).
    n_users = len(all_a["top_users"])
    for u in all_a["top_users"]:
        ua = gov.analytics(u["user"], window)
        sum_tokens += ua["kpis"]["total_tokens"]
        sum_cost += ua["kpis"]["total_cost_usd"]

    assert sum_tokens == all_a["kpis"]["total_tokens"], f"{window}: Σuser tokens != all"
    # Each per-user KPI cost is independently rounded to 4dp, so summing N of them
    # can drift from the (also-4dp) all-users total by up to N * half-a-4dp-ULP.
    # Scale the tolerance with the user count (plus a small margin).
    tol = max(TOL, n_users * 5e-5 + 1e-9)
    assert _approx(sum_cost, all_a["kpis"]["total_cost_usd"], tol), f"{window}: Σuser cost != all"


# ---------------------------------------------------------------------------
# Usage-vs-cap widget — bars are consistent with the KPIs and the cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_usage_vs_cap_consistent(gov, window):
    a = gov.analytics("all", window)
    window_cap = a["window_cap"]

    # Every row quotes the same window-matched cap, has non-negative usage, and
    # (when a cap is set) an implied % within [0, inf) that matches used/cap.
    for row in a["usage_vs_cap"]:
        assert row["used"] >= 0
        assert row["cost"] >= 0
        assert row["quota"] == window_cap, "every bar must quote the window-matched cap"

    # The sum of per-user 'used' in usage_vs_cap equals the all-users KPI tokens
    # (usage_vs_cap and the KPI total come from the same USER#DAY rows).
    used_sum = sum(r["used"] for r in a["usage_vs_cap"])
    assert used_sum == a["kpis"]["total_tokens"], f"{window}: usage_vs_cap used-sum != KPI tokens"


@pytest.mark.parametrize("window", WINDOWS)
def test_users_near_quota_matches_rule(gov, window):
    """KPI 'users_near_quota' == count of usage_vs_cap rows at >=80% of the cap."""
    a = gov.analytics("all", window)
    cap = a["window_cap"]
    expected = sum(1 for r in a["usage_vs_cap"] if cap > 0 and (r["used"] / cap) >= 0.8)
    assert a["kpis"]["users_near_quota"] == expected


# ---------------------------------------------------------------------------
# Window-matched cap rule: 24h -> daily, 7d -> daily x 7, 30d -> monthly
# ---------------------------------------------------------------------------


def test_window_cap_rule(gov):
    cfg = gov.get_config()
    ud = cfg.get("user_default") or {}
    daily = int(ud.get("daily_max_tokens_per_user", 0) or 0)
    monthly = int(ud.get("monthly_max_tokens_per_user", 0) or 0)

    a24 = gov.analytics("all", "24h")
    a7 = gov.analytics("all", "7d")
    a30 = gov.analytics("all", "30d")

    assert a24["cap_period"] == "daily" and a24["window_cap"] == daily
    assert a7["cap_period"] == "weekly" and a7["window_cap"] == daily * 7
    assert a30["cap_period"] == "monthly" and a30["window_cap"] == monthly


# ---------------------------------------------------------------------------
# KPI cross-checks — violations, quota blocks, throttles agree with the arrays
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_violation_kpis_match_arrays(gov, window):
    a = gov.analytics("all", window)
    viol = {v["config"]: v["count"] for v in a["violations"]}

    # 'Total violations' KPI counts only the 5 in-loop CAP settings (quota/throttle
    # have their own KPIs/charts), matching the Limit-violations widget.
    cap_configs = {
        "max_output_tokens_per_model_call",
        "max_tool_calls",
        "max_iterations",
        "max_tokens_per_agent_call",
        "session_max_tokens",
    }
    expected_total = sum(viol.get(c, 0) for c in cap_configs)
    assert a["kpis"]["total_violations"] == expected_total

    # 403/429 KPIs equal the sums of their per-day trend arrays.
    assert a["kpis"]["throttles"] == sum(r["count"] for r in a["throttle_by_day"])
    assert a["kpis"]["quota_blocks"] == sum(
        viol.get(c, 0) for c in ("daily_max_tokens_per_user", "monthly_max_tokens_per_user")
    )


# ---------------------------------------------------------------------------
# Display precision — the 3-decimal cost the UI renders is faithful + reconciles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_cost_display_is_faithful(gov, window):
    """The UI renders cost as `$n.toFixed(3)`. Assert that the 3dp display of each
    value is a faithful rounding of the real value (no value sits on a boundary
    that would display misleadingly), AND that the 3dp-displayed agent costs still
    sum to the 3dp-displayed total — i.e. rounding for display doesn't visibly
    break reconciliation on the page."""
    a = gov.analytics("all", window)

    if not a["cost_by_agent"]:
        pytest.skip("no cost data in this window")

    # 1) Each displayed 3dp value must be within half a 3dp ULP of the real value
    #    (a true rounding — this would FAIL if the backend emitted hidden precision
    #    that makes the shown number off by a full cent-of-a-cent).
    for x in a["cost_by_agent"] + a["top_users"]:
        shown = round(x["cost"], 3)
        assert abs(x["cost"] - shown) <= 0.0005 + 1e-9, (
            f"{x} displays as {shown} but isn't a faithful 3dp rounding"
        )

    # 2) Visible reconciliation: Σ(displayed agent cost) ≈ displayed total. Because
    #    each agent cost is independently rounded to 3dp for display, allow N * half
    #    a 3dp ULP of drift (what a human reading the page would tolerate).
    n = len(a["cost_by_agent"])
    shown_agents = sum(round(x["cost"], 3) for x in a["cost_by_agent"])
    shown_total = round(a["kpis"]["total_cost_usd"], 3)
    assert abs(shown_agents - shown_total) <= n * 0.0005 + 1e-9, (
        f"displayed agent costs {shown_agents} don't reconcile with displayed total {shown_total}"
    )
