#!/usr/bin/env python3
"""Load demo/test data for the app plane, after the infra is deployed.

Data only: the Cognito users are created beforehand by scripts/deploy-app-plane.sh
(with the password scripts/deploy-all.sh asks for). Idempotent. For each user
in seed/data/users.json it:
  1. looks up the user's Cognito `sub` (fails if the user does not exist),
  2. writes that user's orders to the Orders table keyed by `sub`.
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
from shared.flags_schema import ALL_FEATURES  # noqa: E402 -- after the path insert above

_DATA_DIR = Path(__file__).resolve().parent / "data"
# Baseline = every feature OFF. Built from ALL_FEATURES so a newly shipped lever
# is seeded off automatically (the agent reads a missing flag as OFF too, but the
# seed should still reflect the full, current flag set).
_BASELINE_FLAGS = [{"feature": name, "enabled": False, "params": {}} for name in ALL_FEATURES]


def _load(name: str):
    with open(_DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal)  # DynamoDB needs Decimal, not float


def _resolve_sub(cog, pool_id: str, email: str) -> str:
    """Return the user's immutable Cognito `sub` (the canonical user_id / orders key).

    The user must already exist (created by scripts/deploy-app-plane.sh).
    """
    try:
        got = cog.admin_get_user(UserPoolId=pool_id, Username=email)
    except cog.exceptions.UserNotFoundException:
        raise SystemExit(
            f"ERROR: Cognito user {email} not found. It is created by the deploy; run ./scripts/deploy-all.sh"
        ) from None
    for a in got.get("UserAttributes", []):
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
    total_orders = 0
    for u in users:
        sub = _resolve_sub(cog, args.user_pool_id, u["email"])
        n = _write_orders(orders_table, sub, u.get("orders", []))
        total_orders += n
        print(f"  user {u['email']:<18} tier={u.get('tier', 'basic'):<6} sub={sub[:8]}…  orders={n}")

    print(f"  faq   : {_seed_faq(ddb.Table(args.faq_table))} items")
    print(f"  flags : {_seed_flags(ddb.Table(args.flags_table))} items (baseline: all features OFF)")
    print(f"Done. {len(users)} users, {total_orders} orders.")


if __name__ == "__main__":
    main()
