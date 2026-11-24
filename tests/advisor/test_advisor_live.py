"""Live Advisor checks against the deployed stack (opt-in: -m live).

Each requirements §9 question (tests/advisor/questions.json) is asked in a fresh
chat and validated against the document's expectations. The browser walk-through
of the same set is done with Playwright on the admin UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

QS = json.loads((Path(__file__).parent / "questions.json").read_text())
SAVINGS = re.compile(r"\b(would|could|will)\s+(save|cut|reduce)\b[^.]*\d", re.I)


def _check_common(reply: dict) -> None:
    assert reply["status"] == "complete", reply
    assert not re.search(r"\bTKN-\d{2}\b", reply["text"]), "raw catalog id shown"
    assert not SAVINGS.search(reply["text"]), "savings projection"
    for c in reply.get("citation_details") or []:
        assert c["name"] and c["ref_no"] and c["expected_effect"]
    for finding in reply.get("findings") or []:  # the answer's table rows (need not appear in the text)
        assert finding["signal"] and finding["observed"] and finding["level"], finding


@pytest.mark.parametrize("q", QS["questions"], ids=lambda q: q["id"])
def test_section9_question(advisor, q):
    chat_id, reply = advisor.ask(q["question"], QS["window"])
    try:
        _check_common(reply)
        if q["expect_evidence"]:
            assert reply["findings"], f"{q['id']}: expected an evidence block"
            assert {f["level"] for f in reply["findings"]} & set(q["levels"]), reply["findings"]
        if q["id"] == "Q10":
            assert "not recorded" in reply["text"].lower() or "tool-call" in reply["text"].lower()
    finally:
        advisor.delete(chat_id)


def test_cost_split_reconciles_with_analytics(advisor):
    chat_id, reply = advisor.ask("What's the cost split across the three agents over the last 7 days?")
    try:
        assert reply["status"] == "complete", reply
        a = advisor.analytics("7d")
        shown = {
            f["signal"].removesuffix(" cost"): f["value"]
            for f in reply["findings"]
            if f["level"] == "agent" and f["signal"].endswith(" cost")
        }
        # A reply with no agent cost figures must fail, not pass vacuously.
        assert a["cost_by_agent"] and shown, (reply["findings"], a["cost_by_agent"])
        assert {row["agent"] for row in a["cost_by_agent"]} <= shown.keys(), (shown, a["cost_by_agent"])
        for row in a["cost_by_agent"]:
            assert abs(shown[row["agent"]] - row["cost"]) < 1e-6
    finally:
        advisor.delete(chat_id)


@pytest.mark.parametrize("q", QS["out_of_scope"], ids=lambda q: q["id"])
def test_out_of_scope(advisor, q):
    chat_id, reply = advisor.ask(q["question"])
    try:
        assert reply["status"] == "complete"
        if q["expect"] == "decline":
            assert reply["findings"] == [] and reply["citations"] == []
        if q["expect"] == "redirect_governance":
            assert "governance" in reply["text"].lower()
        if q["expect"] == "not_available":
            assert "not available" in reply["text"].lower() or "no outcome" in reply["text"].lower()
    finally:
        advisor.delete(chat_id)


def test_follow_up_resolves_and_history_round_trip(advisor):
    chat_id, first = advisor.ask("Which agent drives the most cost?")
    try:
        _, second = advisor.ask("and the order agent?", chat_id=chat_id)
        assert "order" in second["text"].lower()
        listed = advisor.call("GET", "/advisor/chats").json()["chats"]
        assert chat_id in {c["chat_id"] for c in listed}
    finally:
        advisor.delete(chat_id)
    assert advisor.call("GET", f"/advisor/chats/{chat_id}").status_code == 404
