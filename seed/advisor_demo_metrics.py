"""Seed ~30 days of plausible demo usage into the Metrics table (for trying the Advisor).

The Advisor reads live usage only, so a fresh install has too little history for
period comparisons, outliers or cache trends. This script replays synthetic
`UsageRecorded` events through the REAL metering consumer
(src/control-plane/metering_consumer.py), so every row -- USER#/AGENT#/SESSION#
roll-ups, MODEL# roll-ups, token splits and the TURNS# per-turn ledger -- is
written by exactly the code that records live traffic.

The traffic mirrors what the app plane emits per turn: a Sonnet orchestrator call
plus optional Haiku knowledge/order delegations, priced with shared/price_map.py.
Story in the data (so §9 questions have something to find):
  - days 29..15 prompt cache off; days 14..6 cache on (high hit rate);
    days 5..0 a cache regression (prefix keeps being rewritten)
  - conversation history grows input tokens turn over turn
  - eight demo users; `max-demo` is heavy and has one outlier day
  - one runaway session with a spike turn (several model calls, tool_cap)

Demo identities end in "-demo" (users) / start with "s-<user>-demo" (sessions);
`--clean` deletes exactly those rows. Run with AWS credentials for the account:

    python seed/advisor_demo_metrics.py --region us-west-2            # seed
    python seed/advisor_demo_metrics.py --region us-west-2 --clean    # remove
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

_ROOT = Path(__file__).resolve().parents[1]
SONNET = "global.anthropic.claude-sonnet-5-5"
HAIKU = "us.anthropic.claude-haiku-5-5"
DEMO_MARK = "-demo"

# user -> (probability active on a day, sessions/day range, turns/session range)
USERS = {
    "ana-demo": (0.65, (1, 2), (2, 5)),
    "ben-demo": (0.6, (1, 2), (2, 4)),
    "chen-demo": (0.7, (1, 2), (3, 6)),
    "dia-demo": (0.55, (1, 1), (2, 5)),
    "eli-demo": (0.6, (1, 2), (2, 4)),
    "fay-demo": (0.5, (1, 1), (3, 5)),
    "gus-demo": (0.35, (1, 1), (1, 3)),
    "max-demo": (0.9, (2, 3), (6, 12)),
}
OUTLIER = ("max-demo", 3)  # (user, days ago): ~10 long sessions that day
SPIKE = ("max-demo", 2)  # this user's first session that day runs away (escalating loop turns)


def _stack_output(region: str, key: str) -> str:
    cfn = boto3.client("cloudformation", region_name=region)
    outs = cfn.describe_stacks(StackName="Tokenomics-ControlPlane")["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outs if o["OutputKey"] == key)


def _consumer(region: str, metrics_table: str):
    os.environ.update(
        {
            "AWS_REGION": region,
            "METRICS_TABLE": metrics_table,
            "FLAGS_TABLE": "unused",
            "RUNS_TABLE": "unused",
        }
    )
    for p in (_ROOT / "src", _ROOT / "src" / "control-plane"):
        sys.path.insert(0, str(p))
    import metering_consumer

    return metering_consumer


def _call(cost_usd, model: str, it: int, ot: int, cr: int, cw: int, calls: int = 1) -> dict:
    return {
        "input_tokens": it,
        "output_tokens": ot,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "model_calls": calls,
        "model_id": model,
        "total_input_tokens": it + cr + cw,
        "cost_usd": round(
            cost_usd(model, input_tokens=it, output_tokens=ot, cache_read_tokens=cr, cache_write_tokens=cw), 6
        ),
        "cache_hit_ratio": round(cr / (it + cr + cw), 4) if (it + cr + cw) else 0.0,
    }


def _cache_split(
    rng: random.Random, days_ago: int, turn: int, prefix: int, total_in: int
) -> tuple[int, int, int]:
    """(input, cache_read, cache_write) for one call given the cache phase."""
    if days_ago >= 15:  # cache off
        return total_in, 0, 0
    # healthy (days 6-14): first call writes, then reads; regression (days 0-5): the stable
    # prefix keeps changing -> mostly rewrites, few reads
    rewrite = turn == 1 or (days_ago < 6 and rng.random() < 0.8)
    return (total_in - prefix, 0, prefix) if rewrite else (total_in - prefix, prefix, 0)


def _turn(rng: random.Random, cost_usd, days_ago: int, turn: int, spike: int) -> tuple[dict, dict]:
    history = (turn - 1) * rng.randint(250, 450)  # replayed conversation history
    o_in = rng.randint(3300, 3900) + history
    o_out = rng.randint(110, 320)
    agents = {}
    orch_calls = 1
    if spike:  # runaway: repeated tool loops
        o_in, o_out, orch_calls = o_in * spike, o_out * 3, 1 + spike // 2
    i, cr, cw = _cache_split(rng, days_ago, turn, 2800, o_in)
    agents["orchestrator"] = _call(cost_usd, SONNET, i, o_out, cr, cw, calls=orch_calls)
    for name, p in (("knowledge", 0.55), ("order", 0.4)):
        if rng.random() < p or (spike and name == "order"):
            s_in = rng.randint(1300, 2100)
            i, cr, cw = _cache_split(rng, days_ago, turn, 1100, s_in)
            agents[name] = _call(cost_usd, HAIKU, i, rng.randint(70, 240), cr, cw, calls=3 if spike else 1)
    tot = {
        k: sum(a[k] for a in agents.values())
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "model_calls")
    }
    tot["cost_usd"] = round(sum(a["cost_usd"] for a in agents.values()), 6)
    tot["total_input_tokens"] = tot["input_tokens"] + tot["cache_read_tokens"] + tot["cache_write_tokens"]
    tot["model_id"] = ""
    tot["by_agent"] = agents
    limited = spike or rng.random() < 0.03
    gov = {
        "stopped": False,
        "limited": limited,
        "reason": "tool_cap" if limited else None,
        "scope": "turn" if limited else None,
    }
    return tot, gov


def build_events(days: int, seed: int) -> list[dict]:
    if str(_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(_ROOT / "src"))
    from shared.price_map import cost_usd

    rng = random.Random(seed)
    now = datetime.now(UTC).replace(microsecond=0)
    events = []
    for days_ago in range(days - 1, -1, -1):
        day = now - timedelta(days=days_ago)
        for user, (p_active, sess_rng, turn_rng) in USERS.items():
            outlier = (user, days_ago) == OUTLIER
            if not outlier and rng.random() > p_active:
                continue
            n_sess = 10 if outlier else rng.randint(*sess_rng)
            for s in range(n_sess):
                sid = f"s-{user}-{day:%Y%m%d}-{s + 1}"
                start = day.replace(hour=rng.randint(8, 20), minute=rng.randint(0, 59), second=0)
                if days_ago == 0:
                    start = min(start, now - timedelta(minutes=30))
                runaway = (user, days_ago) == SPIKE and s == 0
                n_turns = 6 if runaway else rng.randint(*turn_rng)
                for t in range(1, n_turns + 1):
                    # escalating loop: turns 3..6 at 4x, 8x, 12x, 16x a normal turn (spike = turn 6)
                    spike = (t - 2) * 4 if runaway and t >= 3 else 0
                    usage, gov = _turn(rng, cost_usd, days_ago, t, spike)
                    ts = (start + timedelta(minutes=2 * t, seconds=rng.randint(0, 59))).isoformat()
                    events.append(
                        {
                            "detail-type": "UsageRecorded",
                            "detail": {
                                "user_id": user,
                                "session_id": sid,
                                "run_id": None,
                                "ts": ts,
                                "flag_config": "prompt_cache=on" if days_ago < 15 else "baseline",
                                "usage": usage,
                                "governance": gov,
                                "model_ids": {"orchestrator": SONNET, "knowledge": HAIKU, "order": HAIKU},
                            },
                        }
                    )
    return events


def seed(region: str, days: int, seed_value: int) -> None:
    table = _stack_output(region, "MetricsTableName")
    mc = _consumer(region, table)
    events = build_events(days, seed_value)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda e: mc.handler(e, None), events))
    failed = sum(1 for r in results if not r.get("ok"))
    users = {e["detail"]["user_id"] for e in events}
    sessions = {e["detail"]["session_id"] for e in events}
    print(
        f"seeded {len(events)} turns, {len(sessions)} sessions, {len(users)} users over {days} days "
        f"into {table} ({failed} failed)"
    )


def clean(region: str) -> None:
    table = boto3.resource("dynamodb", region_name=region).Table(_stack_output(region, "MetricsTableName"))
    keys, kwargs = [], {"ProjectionExpression": "pk"}
    while True:
        page = table.scan(**kwargs)
        keys += [i["pk"] for i in page.get("Items", []) if DEMO_MARK in i["pk"]]
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with table.batch_writer() as batch:
        for pk in keys:
            batch.delete_item(Key={"pk": pk})
    print(f"removed {len(keys)} demo rows")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--clean", action="store_true", help="remove the demo rows instead of seeding")
    args = ap.parse_args()
    if args.clean:
        clean(args.region)
    else:
        seed(args.region, args.days, args.seed)


if __name__ == "__main__":
    main()
