#!/usr/bin/env python3
"""Seed control-plane demo data after deploy: admin user + token-governance tables.

Idempotent. Run at the end of scripts/deploy-control.sh with the admin pool
id and (optionally) the governance table names from the stack outputs.

  1. Admin Cognito user (self sign-up is disabled -> provisioned via admin APIs).
     A NEW admin is created only when TOKENOMICS_ADMIN_PASSWORD is set (it
     becomes the permanent password); otherwise it is skipped with a warning.
     An existing admin's password is only reset when TOKENOMICS_ADMIN_PASSWORD
     is set (see seed/credentials.py). The admin is put in the
     `governance-admin` group (may change config; threat model M14).
     `--viewer-email` optionally provisions a read-only `viewer` user the same
     way, using TOKENOMICS_VIEWER_PASSWORD.
  2. Usage plans (per-agent caps + a global per-user quota default) into the
     shared UsagePlans table, so the Governance page is populated on first load.
     Optionally seeds a per-user OVERRIDE (the live demo lever) by resolving a
     demo customer's Cognito `sub` from the app-plane pool.
  3. A few sample enforcement events into the metrics table so the event log +
     plan-vs-actual aren't empty in a demo.

All governance tables/args are optional: pass only what you have and the rest is
skipped (so this stays compatible with a control-plane-only deploy).

Usage:
    python seed/load_control_plane.py \
        --region us-west-2 --user-pool-id <adminPoolId> \
        [--email admin@example.com] [--viewer-email viewer@example.com] \
        [--usage-plans-table Tokenomics-UsagePlans] \
        [--metrics-table <MetricsTableName>] \
        [--app-user-pool-id <appPoolId> --override-email eva@example.com --override-daily-tokens 500]
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import boto3
import credentials  # sibling module (seed/ is on sys.path when run as a script)

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load(name: str):
    with open(_DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal)  # DynamoDB needs Decimal, not float


def _to_dynamo(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dynamo(v) for v in obj]
    return obj


# ---- admin user ------------------------------------------------------------


_PASSWORD_ENV = "TOKENOMICS_ADMIN_PASSWORD"  # nosec B105 -- env var name, not a password
_VIEWER_PASSWORD_ENV = "TOKENOMICS_VIEWER_PASSWORD"  # nosec B105 -- env var name, not a password
# Cognito groups created by ControlPlaneStack (src/control-plane/lib/authz.py).
ADMIN_GROUP = "governance-admin"
VIEWER_GROUP = "viewer"


def _seed_admin(
    cog,
    pool_id: str,
    email: str,
    password: str | None,
    *,
    group: str = ADMIN_GROUP,
    password_env: str = _PASSWORD_ENV,
) -> None:
    """Create/confirm a control-plane user in `group`; set its password only when supplied.

    A new user with no supplied password is skipped (never created password-less).
    """
    if password is None and not credentials.user_exists(cog, pool_id, email):
        credentials.report_missing(email, password_env)
        return
    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    try:
        cog.admin_create_user(
            UserPoolId=pool_id, Username=email, UserAttributes=attrs, MessageAction="SUPPRESS"
        )
    except cog.exceptions.UsernameExistsException:
        cog.admin_update_user_attributes(UserPoolId=pool_id, Username=email, UserAttributes=attrs)
    if password:
        cog.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)
    cog.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    print(f"  {group} user ready: {email}")


# ---- governance seed -------------------------------------------------------


def _resolve_sub(cog, app_pool_id: str, email: str) -> str | None:
    """Resolve a customer's Cognito sub (for a per-user override demo lever)."""
    try:
        got = cog.admin_get_user(UserPoolId=app_pool_id, Username=email)
    except Exception as exc:  # noqa: BLE001
        print(f"  (override skipped: could not resolve {email}: {type(exc).__name__})")
        return None
    for a in got.get("UserAttributes", []):
        if a["Name"] == "sub":
            return a["Value"]
    return None


def _seed_usage_plans(ddb, table_name: str) -> None:
    """Seed the one global governance config (new schema).

    session_max_tokens is co-located on the user#default row (the schema's
    to_items() convention). No per-user overrides are seeded in V1.
    """
    plans = _load("usage_plans.json")
    table = ddb.Table(table_name)
    items = []
    for name, cfg in (plans.get("agents") or {}).items():
        items.append({"plan_id": f"agent#{name}", "config": cfg})
    default_cfg = dict(plans.get("user_default", {}))
    default_cfg["session_max_tokens"] = plans.get("session_max_tokens", 0)
    items.append({"plan_id": "user#default", "config": default_cfg})

    with table.batch_writer() as bw:
        for it in items:
            bw.put_item(Item=_to_dynamo(it))
    print(f"  usage plans: {len(items)} items into {table_name}")


def _seed_events(ddb, table_name: str) -> None:
    events = _load("governance_events.json")
    table = ddb.Table(table_name)
    with table.batch_writer() as bw:
        for e in events:
            item = dict(e)
            item["pk"] = f"EVENT#{item['ts']}#{uuid.uuid4().hex[:8]}"
            bw.put_item(Item=_to_dynamo(item))
    print(f"  governance events: {len(events)} items into {table_name}")


def _seed_demo_metrics(ddb, table_name: str) -> None:
    """Seed a few analytics roll-up rows so the Governance page isn't empty.

    Writes USER#DAY / AGENT#DAY / SESSION# / VIOLATION# rows for today so the
    KPIs, cost-by-agent, top-users, near-limit and violations widgets populate on
    first load (before any live traffic). Tagged with user_id/day for the GSI.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    ts = datetime.now(UTC).isoformat()
    table = ddb.Table(table_name)

    demo_users = [
        ("demo-alice", 62104, 0.31),
        ("demo-bob", 18930, 0.12),
        ("demo-carol", 9900, 0.05),
    ]
    demo_agents = [("orchestrator", 62104, 0.42), ("order", 18930, 0.06), ("knowledge", 9900, 0.04)]
    demo_violations = [
        ("max_output_tokens_per_model_call", 41),
        ("daily_max_tokens_per_user", 12),
        ("max_tool_calls", 8),
        ("max_iterations", 3),
        ("session_max_tokens", 2),
    ]
    rows: list[dict] = []
    for uid, tok, cost in demo_users:
        rows.append(
            {
                "pk": f"USER#{uid}#DAY#{today}",
                "user_id": uid,
                "day": today,
                "tokens": tok,
                "cost_usd": cost,
                "last_ts": ts,
            }
        )
    for name, tok, cost in demo_agents:
        rows.append(
            {
                "pk": f"AGENT#{name}#DAY#{today}",
                "agent": name,
                "day": today,
                "tokens": tok,
                "cost_usd": cost,
                "last_ts": ts,
            }
        )
    for i in range(6):
        rows.append({"pk": f"SESSION#demo-sess-{i}", "tokens": 8420, "cost_usd": 0.074, "last_ts": ts})
    for cfg, n in demo_violations:
        rows.append(
            {"pk": f"VIOLATION#{cfg}#DAY#{today}", "config": cfg, "day": today, "count": n, "last_ts": ts}
        )

    with table.batch_writer() as bw:
        for r in rows:
            bw.put_item(Item=_to_dynamo(r))
    print(f"  demo metrics: {len(rows)} roll-up rows into {table_name}")


def main() -> None:
    p = argparse.ArgumentParser(description="Seed control-plane demo data.")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--user-pool-id", required=True)
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--viewer-email", default="", help="also provision a read-only viewer user")
    # governance (all optional)
    p.add_argument("--usage-plans-table", default="")
    p.add_argument("--metrics-table", default="")
    # Analytics is EMPTY on a fresh install by default: the Governance page fills
    # in from real chat traffic. Pass --demo-metrics only to pre-populate a demo.
    p.add_argument(
        "--demo-metrics",
        action="store_true",
        help="also seed sample analytics rows (demo only; NOT for clean testing)",
    )
    # Accepted but unused in V1 (no per-user overrides). Kept so the deploy script
    # stays compatible and the future tier feature can wire it back in.
    p.add_argument("--app-user-pool-id", default="")
    p.add_argument("--override-email", default="")
    p.add_argument("--override-daily-tokens", type=int, default=0)
    args = p.parse_args()

    cog = boto3.client("cognito-idp", region_name=args.region)
    ddb = boto3.resource("dynamodb", region_name=args.region)

    print(f"Seeding control-plane data in {args.region}:")
    _seed_admin(cog, args.user_pool_id, args.email, credentials.from_env(_PASSWORD_ENV))
    if args.viewer_email:
        _seed_admin(
            cog,
            args.user_pool_id,
            args.viewer_email,
            credentials.from_env(_VIEWER_PASSWORD_ENV),
            group=VIEWER_GROUP,
            password_env=_VIEWER_PASSWORD_ENV,
        )

    if args.usage_plans_table:
        _seed_usage_plans(ddb, args.usage_plans_table)

    # Analytics (metrics + events) stay EMPTY by default so a fresh install shows
    # no usage until you actually chat. --demo-metrics pre-populates for a demo.
    if args.metrics_table and args.demo_metrics:
        _seed_events(ddb, args.metrics_table)
        _seed_demo_metrics(ddb, args.metrics_table)
        print("  (demo analytics seeded — remove for clean testing)")
    else:
        print("  analytics: left empty (fills from real chat traffic)")

    print("Done.")


if __name__ == "__main__":
    main()
