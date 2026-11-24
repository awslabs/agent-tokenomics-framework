#!/usr/bin/env python3
"""Load demo/test data for the app plane, after the infra is deployed.

Idempotent. For each user in seed/data/users.json it:
  1. creates (or updates) a Cognito user + custom:tier. A NEW user is created
     only when TOKENOMICS_USER_PASSWORD is set (it becomes the permanent
     password); otherwise the user is skipped with a warning. An existing
     user's password is only reset when TOKENOMICS_USER_PASSWORD is set
     (see seed/credentials.py),
  2. captures the user's `sub`,
  3. writes that user's orders to the Orders table keyed by `sub`.
Then loads the FAQ and resets feature flags to the all-off baseline.

Table names + the Cognito user-pool id come from the deployed CloudFormation
outputs (passed as flags by scripts/deploy-app-plane.sh), so nothing is
hardcoded. Re-running updates rather than duplicating.

Usage:
    python seed/load_app_plane.py \
        --region us-west-2 \
        --user-pool-id <id> \
        --orders-table Tokenomics-Orders \
        --faq-table Tokenomics-Faq \
        --flags-table Tokenomics-Flags
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import boto3

_ROOT = Path(__file__).resolve().parents[1]
# Reach the shared flag schema so the baseline seed is driven by ALL_FEATURES
# (the single source of truth) -- never a hand-kept list that drifts as levers ship.
sys.path.insert(0, str(_ROOT / "src"))
import credentials  # noqa: E402 -- sibling module (seed/ is on sys.path when run as a script)

from shared.flags_schema import ALL_FEATURES  # noqa: E402 -- after the path insert above

_DATA_DIR = Path(__file__).resolve().parent / "data"
# Baseline = every feature OFF. Built from ALL_FEATURES so a newly shipped lever
# is seeded off automatically (the agent reads a missing flag as OFF too, but the
# seed should still reflect the full, current flag set).
_BASELINE_FLAGS = [{"feature": name, "enabled": False, "params": {}} for name in ALL_FEATURES]


def _load(name: str):
    with open(_DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal)  # DynamoDB needs Decimal, not float


_PASSWORD_ENV = "TOKENOMICS_USER_PASSWORD"  # nosec B105 -- env var name, not a password


def _upsert_cognito_user(cog, pool_id: str, user: dict, password: str | None) -> str | None:
    """Create/confirm a Cognito user; return the sub, or None if it was skipped.

    `password` is the operator-supplied one (or None). A new user is created only
    when a password is supplied; an existing user's password is left alone unless
    one is supplied.
    """
    email = user["email"]
    if password is None and not credentials.user_exists(cog, pool_id, email):
        credentials.report_missing(email, _PASSWORD_ENV)
        return None
    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "name", "Value": user.get("name", email)},
        {"Name": "custom:tier", "Value": user.get("tier", "basic")},
    ]
    try:
        resp = cog.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=attrs,
            MessageAction="SUPPRESS",  # no invitation email for demo users
        )
        created = resp["User"]
    except cog.exceptions.UsernameExistsException:
        # Already there -> update attributes, keep going (idempotent).
        cog.admin_update_user_attributes(UserPoolId=pool_id, Username=email, UserAttributes=attrs)
        created = cog.admin_get_user(UserPoolId=pool_id, Username=email)

    # Permanent password so the user can log in immediately (no forced reset flow).
    if password:
        cog.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=password, Permanent=True)

    # Resolve the immutable `sub` (the canonical user_id / orders key).
    got = cog.admin_get_user(UserPoolId=pool_id, Username=email)
    for a in got.get("UserAttributes", created.get("Attributes", [])):
        if a["Name"] == "sub":
            return a["Value"]
    raise RuntimeError(f"could not resolve sub for {email}")


def _write_orders(orders_table, user_id: str, orders: list) -> int:
    with orders_table.batch_writer() as bw:
        for o in orders:
            item = dict(o)
            item["user_id"] = user_id
            bw.put_item(Item=item)
    return len(orders)


def _seed_faq(faq_table) -> int:
    items = _load("faq.json")
    with faq_table.batch_writer() as bw:
        for it in items:
            bw.put_item(Item=it)
    return len(items)


def _seed_flags(flags_table) -> int:
    with flags_table.batch_writer() as bw:
        for it in _BASELINE_FLAGS:
            bw.put_item(Item=it)
    return len(_BASELINE_FLAGS)


def main() -> None:
    p = argparse.ArgumentParser(description="Load app-plane demo data (Cognito + DynamoDB).")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--user-pool-id", required=True)
    p.add_argument("--orders-table", required=True)
    p.add_argument("--faq-table", required=True)
    p.add_argument("--flags-table", required=True)
    args = p.parse_args()

    cog = boto3.client("cognito-idp", region_name=args.region)
    ddb = boto3.resource("dynamodb", region_name=args.region)
    orders_table = ddb.Table(args.orders_table)

    print(f"Loading app-plane demo data in {args.region}:")
    users = _load("users.json")
    password = credentials.from_env(_PASSWORD_ENV)
    total_orders = 0
    skipped = 0
    for u in users:
        sub = _upsert_cognito_user(cog, args.user_pool_id, u, password)
        if sub is None:
            skipped += 1
            continue
        n = _write_orders(orders_table, sub, u.get("orders", []))
        total_orders += n
        print(f"  user {u['email']:<18} tier={u.get('tier', 'basic'):<6} sub={sub[:8]}…  orders={n}")

    print(f"  faq   : {_seed_faq(ddb.Table(args.faq_table))} items")
    print(f"  flags : {_seed_flags(ddb.Table(args.flags_table))} items (baseline: all features OFF)")
    print(f"Done. {len(users) - skipped} users, {total_orders} orders.")
    if skipped:
        print(f"  {skipped} user(s) skipped: set {_PASSWORD_ENV} and re-run.")


if __name__ == "__main__":
    main()
