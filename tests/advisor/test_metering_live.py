"""Live check that app-plane chat turns from two users land in the Metrics table the
way the Advisor needs them, and that the Advisor reads them back (opt-in: -m live).
Credentials come only from the environment:

    TOKENOMICS_USER_EMAIL / TOKENOMICS_USER_PASSWORD     a chat (app-plane) user
    TOKENOMICS_USER2_EMAIL / TOKENOMICS_USER2_PASSWORD   a second chat user
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime

import boto3
import pytest
import requests
from conftest import APP_STACK, REGION, login, outputs

pytestmark = pytest.mark.live

SONNET = "global.anthropic.claude-sonnet-5-5"
USER2 = ("TOKENOMICS_USER2_EMAIL", "TOKENOMICS_USER2_PASSWORD")
SPLIT = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _chat(app: dict[str, str], token: str, conv: str, messages: list[str]) -> dict[str, str]:
    """Send the messages as one conversation (same conversation_id = one session)."""
    ident = {}
    for msg in messages:
        r = requests.post(
            f"{app['ChatApiUrl'].rstrip('/')}/chat",
            timeout=150,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": msg, "conversation_id": conv},
        )
        assert r.status_code == 200, r.text
        ident = r.json()["identity"]
    return ident


def _num(row: dict | None, attr: str) -> float:
    return float((row or {}).get(attr, 0) or 0)


def _wait_turns(table, sid: str, n: int) -> dict:
    row = None
    for _ in range(45):  # EventBridge -> consumer is asynchronous
        row = table.get_item(Key={"pk": f"TURNS#{sid}"}).get("Item")
        if row and len(row.get("turn_log") or []) >= n:
            return row
        time.sleep(2)
    raise AssertionError(f"TURNS#{sid} has {len((row or {}).get('turn_log') or [])} of {n} turns")


def test_two_users_single_and_multi_turn_sessions_are_metered_and_visible_to_the_advisor(advisor):
    creds = [
        (os.environ.get("TOKENOMICS_USER_EMAIL"), os.environ.get("TOKENOMICS_USER_PASSWORD")),
        (os.environ.get(USER2[0]), os.environ.get(USER2[1])),
    ]
    if not all(e and p for e, p in creds):
        pytest.skip("set TOKENOMICS_USER_EMAIL/PASSWORD and TOKENOMICS_USER2_EMAIL/PASSWORD (two chat users)")
    app = outputs(APP_STACK)
    table = boto3.resource("dynamodb", region_name=REGION).Table(outputs()["MetricsTableName"])
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    tok1, tok2 = (login(app["UserPoolId"], app["UserPoolClientId"], *c) for c in creds)
    run = uuid.uuid4().hex[:8]
    plan = [  # (token, conversation, messages)
        (tok1, f"adv-2u-{run}-a", ["What is your return policy?"]),  # single turn
        (
            tok2,
            f"adv-2u-{run}-b",
            ["Where is my order 1001?", "When will it arrive?", "Can I change the delivery address for it?"],
        ),  # 3 turns
        (tok1, f"adv-2u-{run}-c", ["Do you ship internationally?", "How long does that take?"]),  # 2 turns
    ]
    subs = {}
    before: dict[str, dict] = {}
    sessions = []
    for tok, conv, msgs in plan:
        # identity (and so the user's sub) is only known after the first reply
        ident = _chat(app, tok, conv, msgs[:1])
        sub = ident["user_id"]
        if sub not in before:
            # Snapshot after this user's first turn is metered (the consumer writes USER# before
            # TURNS#), then subtract that turn below via the ledger.
            _wait_turns(table, ident["session_id"], 1)
            before[sub] = {
                "user": table.get_item(Key={"pk": f"USER#{sub}#DAY#{day}"}).get("Item"),
                "first_sid": ident["session_id"],
            }
        if len(msgs) > 1:
            _chat(app, tok, conv, msgs[1:])
        subs[ident["session_id"]] = sub
        sessions.append((ident["session_id"], sub, len(msgs)))

    ledgers = {}
    for sid, sub, n in sessions:
        led = _wait_turns(table, sid, n)
        ledgers[sid] = led
        log = led["turn_log"]
        assert len(log) == n and len({e["ts"] for e in log}) == n  # one entry per turn, no duplicates
        assert [e["ts"] for e in log] == sorted(e["ts"] for e in log)
        assert int(led["ttl"]) > time.time()
        for e in log:
            assert int(e["tokens"]) == sum(int(e[k]) for k in SPLIT) > 0  # split adds up to the turn total
            assert int(e["agents"]["orchestrator"]["model_calls"]) >= 1
            assert sum(float(a["cost_usd"]) for a in e["agents"].values()) == pytest.approx(
                float(e["cost_usd"]), rel=1e-6
            )
        sess = table.get_item(Key={"pk": f"SESSION#{sid}"}).get("Item")
        assert int(sess["turns"]) == n and sess["user_id"] == sub and int(sess["input_tokens"]) > 0
        assert int(sess["tokens"]) == sum(int(e["tokens"]) for e in log)  # session roll-up == its ledger
        assert float(sess["cost_usd"]) == pytest.approx(sum(float(e["cost_usd"]) for e in log), rel=1e-6)
        for e in log:  # per-agent tokens add up to the turn
            assert sum(int(a["tokens"]) for a in e["agents"].values()) == int(e["tokens"]), e

    for sub, b in before.items():
        mine = [(sid, n) for sid, s, n in sessions if s == sub]
        first_turn = int(ledgers[b["first_sid"]]["turn_log"][0]["tokens"])
        after = table.get_item(Key={"pk": f"USER#{sub}#DAY#{day}"}).get("Item")
        added = sum(int(e["tokens"]) for sid, _ in mine for e in ledgers[sid]["turn_log"]) - first_turn
        assert (
            _num(after, "tokens") - _num(b["user"], "tokens") == added
        )  # user/day roll-up moved by exactly our turns
        assert _num(after, "turns") - _num(b["user"], "turns") == sum(n for _, n in mine) - 1
        agent = table.get_item(Key={"pk": f"AGENT#orchestrator#USER#{sub}#DAY#{day}"}).get("Item")
        assert agent and agent["model_id"] == SONNET and int(agent["input_tokens"]) > 0
        model = table.get_item(Key={"pk": f"MODEL#{SONNET}#USER#{sub}#DAY#{day}"}).get("Item")
        assert model and float(model["cost_usd"]) > 0 and model["source"] == "live"

    # ---- the Advisor sees these sessions: a question, then follow-ups in the same chat ----
    eva_sid = sessions[1][0]
    eva_log = ledgers[eva_sid]["turn_log"]
    chat_id, reply = advisor.ask(f"Show me the turn-by-turn cost curve for session {eva_sid}.", window="24h")
    try:
        assert reply["status"] == "complete", reply
        figs = {f["signal"]: f["value"] for f in reply["findings"]}
        for i, e in enumerate(eva_log, start=1):
            # each turn shows up (as tokens -- or as the spike turn -- and/or cost) with the ledger's values
            toks = [figs[k] for k in (f"Turn {i} tokens", f"Spike turn {i} tokens") if k in figs]
            cost = figs.get(f"Turn {i} cost")
            assert toks or cost is not None, (i, figs)
            assert all(t == int(e["tokens"]) for t in toks), (i, toks, e["tokens"])
            assert cost is None or cost == pytest.approx(float(e["cost_usd"]), rel=1e-6), (i, cost)
        assert not any(k.startswith(("Turn 4 ", "Spike turn 4 ")) for k in figs)  # exactly the 3 turns

        email2 = creds[1][0]
        _, reply = advisor.ask(
            f"How does {email2} compare to a typical user over the last 24 hours?",
            window="24h",
            chat_id=chat_id,
        )
        assert reply["status"] == "complete", reply
        figs = {f["signal"]: f["value"] for f in reply["findings"]}
        assert figs.get(f"{email2} turns", 0) >= 3 and figs.get(f"{email2} sessions", 0) >= 1, figs

        email1 = creds[0][0]
        _, reply = advisor.ask(f"And {email1}?", window="24h", chat_id=chat_id)
        assert reply["status"] == "complete", reply
        figs = {f["signal"]: f["value"] for f in reply["findings"]}
        assert figs.get(f"{email1} turns", 0) >= 3 and figs.get(f"{email1} sessions", 0) >= 2, figs
    finally:
        advisor.delete(chat_id)
