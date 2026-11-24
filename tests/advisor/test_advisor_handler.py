"""Unit tests for the Advisor API, worker, chat store and runtime client, no AWS.

Run from the repo root:
    src/app-plane/.venv/bin/python -m pytest tests/advisor/test_advisor_handler.py -q
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

_ROOT = Path(__file__).resolve().parents[2]

A, B = "sub-admin-a", "sub-admin-b"
CHAT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def m(monkeypatch, cp_import):
    monkeypatch.setenv("ADVISOR_CHATS_TABLE", "chats")
    monkeypatch.setenv("ADVISOR_WORKER_FUNCTION_NAME", "worker-fn")
    monkeypatch.setenv("ADVISOR_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/adv")
    names = (
        "advisor.chats",
        "advisor.client",
        "advisor.grounding",
        "advisor.api",
        "advisor.worker",
        "lambda_entry",
    )
    return dict(zip(names, cp_import(*names)))


# ---- in-memory chat store (the module's contract, keyed strictly by owner) -------------


class FakeChats:
    def __init__(self, chats_mod):
        self.mod = chats_mod
        self.db: dict[str, dict[str, dict]] = {}  # owner -> sk -> item
        self.finished: list = []

    def install(self, monkeypatch, target):
        for fn in (
            "start_turn",
            "claim_reply",
            "finish_reply",
            "messages",
            "list_chats",
            "get_chat",
            "delete_chat",
            "delete_all",
        ):
            monkeypatch.setattr(target, fn, getattr(self, fn))

    def start_turn(self, owner, chat_id, message, window, user, new_chat, ids):
        rows = self.db.setdefault(owner, {})
        hdr = rows.get(f"CHAT#{chat_id}")
        if new_chat:
            rows[f"CHAT#{chat_id}"] = hdr = {"title": message[:80]}
        if hdr is None:
            raise self.mod.ChatNotFound(chat_id)
        if hdr.get("pending_msg"):
            raise self.mod.ChatBusy(chat_id)
        n = len(rows)
        u_sk, r_sk = f"MSG#{chat_id}#{n:04d}#0#{ids[0]}", f"MSG#{chat_id}#{n:04d}#1#{ids[1]}"
        rows[u_sk] = {"sk": u_sk, "role": "user", "text": message, "status": "complete"}
        rows[r_sk] = {
            "sk": r_sk,
            "role": "assistant",
            "text": "",
            "status": "pending",
            "request_text": message,
            "window": window,
            "user_focus": user,
        }
        hdr["pending_msg"] = r_sk
        return {"chat_id": chat_id, "message_id": ids[1], "reply_sk": r_sk}

    def claim_reply(self, owner, sk):
        it = self.db.get(owner, {}).get(sk)
        if not it or it["status"] != "pending" or it.get("claimed"):
            return None
        it["claimed"] = True
        return dict(it)

    def finish_reply(self, owner, chat_id, sk, status, result):
        self.finished.append((owner, chat_id, sk, status, result))
        it = self.db.get(owner, {}).get(sk)
        if not it or it["status"] != "pending":
            return False
        it.update(status=status, text=result.get("reply", ""), findings=result.get("findings", []))
        self.db[owner][f"CHAT#{chat_id}"].pop("pending_msg", None)
        return True

    def messages(self, owner, chat_id):
        return sorted(
            (v for k, v in self.db.get(owner, {}).items() if k.startswith(f"MSG#{chat_id}#")),
            key=lambda x: x["sk"],
        )

    def list_chats(self, owner):
        return [{"chat_id": k.split("#", 1)[1]} for k in self.db.get(owner, {}) if k.startswith("CHAT#")]

    def get_chat(self, owner, chat_id):
        if f"CHAT#{chat_id}" not in self.db.get(owner, {}):
            raise self.mod.ChatNotFound(chat_id)
        return {"chat_id": chat_id, "messages": self.messages(owner, chat_id)}

    def delete_chat(self, owner, chat_id):
        rows = self.db.get(owner, {})
        if f"CHAT#{chat_id}" not in rows:
            raise self.mod.ChatNotFound(chat_id)
        for k in [k for k in rows if k == f"CHAT#{chat_id}" or k.startswith(f"MSG#{chat_id}#")]:
            del rows[k]

    def delete_all(self, owner):
        n = sum(1 for k in self.db.get(owner, {}) if k.startswith("CHAT#"))
        self.db.pop(owner, None)
        return n


def api(m, method, path, body=None, sub=A):
    ev = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        # The Advisor is open to read-only viewers too (lib/authz.py).
        "requestContext": {"authorizer": {"claims": {"sub": sub, "cognito:groups": "viewer"}} if sub else {}},
    }
    r = m["lambda_entry"].handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


@pytest.fixture
def wired(m, monkeypatch):
    fake = FakeChats(m["advisor.chats"])
    fake.install(monkeypatch, m["advisor.api"].chats)
    fake.install(monkeypatch, m["advisor.worker"].chats)
    invokes = []

    class L:
        def invoke(self, **kw):
            invokes.append(kw)

    monkeypatch.setattr(m["advisor.api"], "_lambda_client", lambda: L())
    return fake, invokes


# ---- API ----------------------------------------------------------------------------------


def test_send_creates_chat_and_invokes_worker(m, wired):
    fake, invokes = wired
    code, body = api(m, "POST", "/advisor/chats", {"message": "Which agent costs the most?"})
    assert code == 202 and body["status"] == "pending"
    chat_id = body["chat_id"]
    assert invokes[0]["InvocationType"] == "Event" and invokes[0]["FunctionName"] == "worker-fn"
    payload = json.loads(invokes[0]["Payload"])
    assert payload["owner"] == A and payload["chat_id"] == chat_id
    reply = fake.db[A][payload["reply_sk"]]
    assert reply["status"] == "pending" and reply["window"] == "7d" and reply["user_focus"] == "all"


def test_owner_comes_from_claims_not_body(m, wired):
    fake, invokes = wired
    api(m, "POST", "/advisor/chats", {"message": "hi", "owner": B, "sub": B})
    assert list(fake.db) == [A]
    assert api(m, "POST", "/advisor/chats", {"message": "hi"}, sub=None)[0] == 403


def test_second_send_while_pending_is_409(m, wired):
    _, _ = wired
    _, body = api(m, "POST", "/advisor/chats", {"message": "q1"})
    code, err = api(m, "POST", "/advisor/chats", {"message": "q2", "chat_id": body["chat_id"]})
    assert code == 409 and "still answering" in err["error"]


def test_bad_body_400_and_unknown_chat_404(m, wired):
    assert api(m, "POST", "/advisor/chats", {"message": ""})[0] == 400
    assert api(m, "POST", "/advisor/chats", {"message": "q", "window": "1y"})[0] == 400
    assert api(m, "POST", "/advisor/chats", {"message": "q", "chat_id": CHAT})[0] == 404
    assert api(m, "GET", "/advisor/chats/not-a-uuid")[0] == 404


def test_chat_isolation_between_admins(m, wired):
    fake, _ = wired
    _, body = api(m, "POST", "/advisor/chats", {"message": "mine"}, sub=A)
    cid = body["chat_id"]
    assert api(m, "GET", f"/advisor/chats/{cid}", sub=B)[0] == 404
    assert api(m, "DELETE", f"/advisor/chats/{cid}", sub=B)[0] == 404
    assert api(m, "DELETE", "/advisor/chats", sub=B) == (200, {"deleted": 0})
    assert api(m, "GET", f"/advisor/chats/{cid}", sub=A)[0] == 200
    assert api(m, "GET", "/advisor/chats", sub=B) == (200, {"chats": []})


def test_list_delete_one_delete_all(m, wired):
    fake, _ = wired
    ids = [api(m, "POST", "/advisor/chats", {"message": f"q{i}"})[1]["chat_id"] for i in range(3)]
    assert len(api(m, "GET", "/advisor/chats")[1]["chats"]) == 3
    assert api(m, "DELETE", f"/advisor/chats/{ids[0]}") == (200, {"deleted": ids[0]})
    assert api(m, "DELETE", "/advisor/chats") == (200, {"deleted": 2})
    assert api(m, "GET", "/advisor/chats")[1] == {"chats": []}


def test_worker_invoke_failure_marks_reply_failed(m, wired, monkeypatch):
    fake, _ = wired

    class Broken:
        def invoke(self, **kw):
            raise RuntimeError("throttled")

    monkeypatch.setattr(m["advisor.api"], "_lambda_client", lambda: Broken())
    code, body = api(m, "POST", "/advisor/chats", {"message": "q"})
    assert code == 202
    assert fake.finished[-1][3] == "failed" and "pending_msg" not in fake.db[A][f"CHAT#{body['chat_id']}"]


def test_storage_errors_never_leak_internals(m, wired, monkeypatch):
    def boom(owner):
        raise ClientError(
            {
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "Requested resource not found: Table: Tokenomics-ControlPlane-AdvisorChats-X",
                }
            },
            "Query",
        )

    monkeypatch.setattr(m["advisor.api"].chats, "list_chats", boom)
    code, body = api(m, "GET", "/advisor/chats")
    assert (
        code == 500
        and "AdvisorChats" not in body["error"]
        and body["error"] == "internal error"  # generic 500 (lambda_entry, M15)
    )


# ---- worker -------------------------------------------------------------------------------


def _start(m, fake, text="Which agent costs the most?"):
    st = fake.start_turn(A, CHAT, text, "7d", "all", True, ("u1", "r1"))
    return {"owner": A, "chat_id": CHAT, "reply_sk": st["reply_sk"]}


def _answer(narrative, patterns=(), calls=None, table=()):
    return {
        "outcome": "answered",
        "narrative": narrative,
        "patterns_applied": list(patterns),
        "table": list(table),
        "tool_calls": calls
        if calls is not None
        else [
            {
                "ref": "t1",
                "tool": "get_cost_summary",
                "result": {
                    "figures": {
                        "total.cost": {
                            "value": 2.6,
                            "display": "$2.60",
                            "signal": "Total cost",
                            "level": "fleet",
                            "headline": True,
                        }
                    }
                },
            }
        ],
        "stop_reason": "end_turn",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadInputTokens": 900,
            "cacheWriteInputTokens": 50,
        },
    }


def test_worker_grounds_and_completes(m, wired, monkeypatch, capsys):
    fake, _ = wired
    ev = _start(m, fake)
    seen = {}

    def inv(payload, sid):
        seen.update(payload=payload, sid=sid)
        return _answer("Total cost is [[t1.total.cost]].", table=["[[t1.total.cost]]"])

    monkeypatch.setattr(m["advisor.worker"].advisor_client, "invoke", inv)
    assert m["advisor.worker"].handler(ev, None) == {"ok": True}
    owner, chat, sk, status, result = fake.finished[-1]
    assert status == "complete" and result["reply"] == "Total cost is $2.60." and result["grounded"]
    assert [finding["observed"] for finding in result["findings"]] == ["$2.60"]  # the answer's table
    assert seen["payload"] == {
        "message": "Which agent costs the most?",
        "history": [],
        "window": "7d",
        "user": "all",
    }
    assert seen["sid"] == f"advisor-{A}-{CHAT}"
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["event"] == "advisor_turn" and log["tools"] == ["get_cost_summary"] and log["grounded"] is True
    assert (log["cache_read_tokens"], log["cache_write_tokens"], log["findings"]) == (900, 50, 1)
    assert "Which agent" not in json.dumps(log) and "$2.60" not in json.dumps(log)  # no prompt/reply text


def test_worker_drop_path_and_history(m, wired, monkeypatch):
    fake, _ = wired
    ev = _start(m, fake)
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client, "invoke", lambda p, s: _answer("Total cost is $9.99.")
    )
    m["advisor.worker"].handler(ev, None)
    gr = m["advisor.grounding"]
    result = fake.finished[-1][4]
    assert result["reply"] == gr.NOTE_UNVERIFIED and result["findings"][0]["observed"] == "$2.60"
    # second turn: history carries the first completed pair only
    st = fake.start_turn(A, CHAT, "and the order agent?", "7d", "all", False, ("u2", "r2"))
    seen = {}
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client, "invoke", lambda p, s: seen.update(p) or _answer("Fine.")
    )
    m["advisor.worker"].handler({"owner": A, "chat_id": CHAT, "reply_sk": st["reply_sk"]}, None)
    assert seen["history"] == [
        {"role": "user", "text": "Which agent costs the most?"},
        {"role": "assistant", "text": gr.NOTE_UNVERIFIED},
    ]


class _Ctx:
    def __init__(self, secs):
        self.secs = secs

    def get_remaining_time_in_millis(self):
        return int(self.secs * 1000)


def test_worker_retries_once_with_the_rejection_and_a_shorter_deadline(m, wired, monkeypatch, capsys):
    fake, _ = wired
    ev = _start(m, fake)
    seen = []

    def inv(payload, sid, read_timeout=None):
        seen.append(payload)
        timeouts.append(read_timeout)
        return _answer(
            'Total cost is 16× "higher".' if len(seen) == 1 else "Total cost is [[t1.total.cost]]."
        )

    timeouts = []
    monkeypatch.setattr(m["advisor.worker"].advisor_client, "invoke", inv)
    m["advisor.worker"].handler(ev, _Ctx(150))
    assert len(seen) == 2 and "retry_note" not in seen[0]
    assert timeouts == [None, 135]  # retry read timeout = remaining 150 s - 15 s
    assert seen[1]["retry_note"].startswith('A previous draft was not shown because it typed "16×" itself')
    assert '\\"' not in seen[1]["retry_note"] and seen[1]["deadline"] == 90
    assert fake.finished[-1][4]["reply"] == "Total cost is $2.60."
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["retried"] == "grounded:typed_figure:16×"


def test_worker_counts_kb_fallbacks_across_both_attempts(m, wired, monkeypatch, capsys):
    fake, _ = wired
    ev = _start(m, fake)
    cost = {
        "ref": "t1",
        "tool": "get_cost_summary",
        "result": {
            "figures": {
                "total.cost": {
                    "value": 2.6,
                    "display": "$2.60",
                    "signal": "Total cost",
                    "level": "fleet",
                    "headline": True,
                }
            }
        },
    }

    def calls(source):
        return [cost, {"ref": "t2", "tool": "search_patterns", "result": {"source": source, "patterns": []}}]

    answers = iter(
        [
            _answer("Total cost is $9.99.", calls=calls("fallback")),  # rejected, then discarded
            _answer("Total cost is [[t1.total.cost]].", calls=calls("kb")),
        ]
    )
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client, "invoke", lambda p, s, read_timeout=None: next(answers)
    )
    m["advisor.worker"].handler(ev, _Ctx(150))
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["retried"].startswith("grounded:") and log["KbFallbacks"] == 1
    emf = log["_aws"]["CloudWatchMetrics"][0]
    assert (
        emf
        == {
            "Namespace": "Tokenomics/Advisor",
            "Dimensions": [[]],
            "Metrics": [{"Name": "KbFallbacks", "Unit": "Count"}],
        }
        and log["_aws"]["Timestamp"] > 0
    )


def test_worker_retries_an_answer_that_ran_out_of_steps(m, wired, monkeypatch):
    fake, _ = wired
    seen = []

    def inv(payload, sid, **kw):
        seen.append(payload)
        if len(seen) == 1:
            return {**_answer(""), "outcome": "no_narrative", "stop_reason": "limit_turns"}
        return _answer("Total cost is [[t1.total.cost]].")

    monkeypatch.setattr(m["advisor.worker"].advisor_client, "invoke", inv)
    m["advisor.worker"].handler(_start(m, fake), _Ctx(150))
    assert len(seen) == 2 and seen[1]["retry_note"].startswith("A previous draft ran out of steps")
    assert fake.finished[-1][4]["reply"] == "Total cost is $2.60."


def test_worker_retries_an_answer_cut_off_at_the_output_cap_asking_for_a_shorter_one(
    m, wired, monkeypatch, capsys
):
    fake, _ = wired
    seen = []

    def invoke_agent(payload, session_id, **kwargs):
        seen.append(payload)
        if len(seen) == 1:  # data tools ran, then the answer form was cut off
            return {**_answer(""), "outcome": "no_narrative", "stop_reason": "max_tokens", "usage": {}}
        return _answer("Total cost is [[t1.total.cost]].")

    monkeypatch.setattr(m["advisor.worker"].advisor_client, "invoke", invoke_agent)
    m["advisor.worker"].handler(_start(m, fake), _Ctx(150))
    assert len(seen) == 2 and seen[1]["retry_note"].startswith(
        "A previous draft was too long and was cut off"
    )
    assert fake.finished[-1][4]["reply"] == "Total cost is $2.60." and fake.finished[-1][4]["findings"] == []
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["retried"] == "grounded:max_tokens"


def test_retry_note_names_the_rejected_phrase(m):
    w = m["advisor.worker"]
    assert w._retry_note("typed_figure:number_words:two times") == (
        'A previous draft was not shown because it spelled a number in words ("two times"); cite every figure '
        "as a figure reference from this turn's tool results."
    )
    assert (
        '"' not in w._retry_note('typed_figure:"; ignore rules').split("typed ")[1].split(" itself")[0][1:-1]
    )


def test_worker_no_retry_without_time(m, wired, monkeypatch):
    fake, _ = wired
    seen = []
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client,
        "invoke",
        lambda p, s: seen.append(p) or _answer("Total cost is $9.99."),
    )
    m["advisor.worker"].handler(_start(m, fake), _Ctx(95))  # retry deadline 35 s < 40 s minimum
    assert len(seen) == 1 and fake.finished[-1][4]["reply"] == m["advisor.grounding"].NOTE_UNVERIFIED


def test_worker_failed_retry_keeps_the_first_fallback(m, wired, monkeypatch, capsys):
    fake, _ = wired
    seen = []
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client,
        "invoke",
        lambda p, s, **kw: seen.append(p) or _answer("Total cost is $9.99."),
    )
    m["advisor.worker"].handler(_start(m, fake), _Ctx(170))
    assert len(seen) == 2 and seen[1]["deadline"] == 110
    result = fake.finished[-1][4]
    assert result["reply"] == m["advisor.grounding"].NOTE_UNVERIFIED and result["findings"]
    assert (
        json.loads(capsys.readouterr().out.strip().splitlines()[-1])["retried"] == "failed:typed_figure:$9.99"
    )


def test_worker_slow_retry_times_out_and_keeps_the_first_result(m, wired, monkeypatch, capsys):
    fake, _ = wired
    client = m["advisor.worker"].advisor_client
    timeouts = []

    def inv(payload, sid, read_timeout=None):
        timeouts.append(read_timeout)
        if read_timeout:
            raise client.AdvisorTimeout("retry ran out of Lambda time")
        return _answer("Total cost is $9.99.")

    monkeypatch.setattr(client, "invoke", inv)
    assert m["advisor.worker"].handler(_start(m, fake), _Ctx(170)) == {"ok": True}
    assert timeouts == [None, 155]
    owner, chat, sk, status, result = fake.finished[-1]
    assert (
        status == "complete"
        and result["reply"] == m["advisor.grounding"].NOTE_UNVERIFIED
        and result["findings"]
    )
    assert (
        json.loads(capsys.readouterr().out.strip().splitlines()[-1])["retried"] == "failed:typed_figure:$9.99"
    )


def test_worker_fails_an_expired_reply_without_invoking(m, wired, monkeypatch, capsys):
    fake, _ = wired
    ev = _start(m, fake)
    calls = []
    w = m["advisor.worker"]

    def expired(owner, sk):
        raise w.chats.ReplyExpired(sk)

    monkeypatch.setattr(w.chats, "claim_reply", expired)
    monkeypatch.setattr(w.advisor_client, "invoke", lambda *a, **kw: calls.append(1) or _answer("ok"))
    assert w.handler(ev, _Ctx(170)) == {"ok": False}
    assert calls == []
    assert (
        fake.finished[-1][3] == "failed"
        and fake.finished[-1][4]["reply"] == m["advisor.grounding"].NOTE_FAILED
    )
    assert "pending_msg" not in fake.db[A][f"CHAT#{CHAT}"]
    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["outcome"] == "expired" and log["stored"] is True


def test_worker_duplicate_delivery_refused(m, wired, monkeypatch):
    fake, _ = wired
    ev = _start(m, fake)
    calls = []
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client, "invoke", lambda p, s: calls.append(1) or _answer("ok")
    )
    m["advisor.worker"].handler(ev, None)
    assert m["advisor.worker"].handler(ev, None) == {"ok": True, "skipped": True}
    assert calls == [1]


@pytest.mark.parametrize(
    "data,expect",
    [
        ({"outcome": "declined", "tool_calls": []}, "NOTE_DECLINED"),
        ({"outcome": "timeout", "tool_calls": []}, "NOTE_TIMEOUT"),
        ({"outcome": "no_narrative", "tool_calls": []}, "NOTE_FAILED"),
    ],
)
def test_worker_non_answer_outcomes(m, wired, monkeypatch, data, expect):
    fake, _ = wired
    ev = _start(m, fake)
    monkeypatch.setattr(m["advisor.worker"].advisor_client, "invoke", lambda p, s: data)
    m["advisor.worker"].handler(ev, None)
    assert fake.finished[-1][4]["reply"] == getattr(m["advisor.grounding"], expect)
    assert fake.finished[-1][4]["findings"] == []


def test_worker_runtime_timeout_and_error(m, wired, monkeypatch):
    fake, _ = wired
    client = m["advisor.worker"].advisor_client
    ev = _start(m, fake)
    monkeypatch.setattr(client, "invoke", lambda p, s: (_ for _ in ()).throw(client.AdvisorTimeout("t")))
    m["advisor.worker"].handler(ev, None)
    assert fake.finished[-1][3] == "complete" and fake.finished[-1][4]["reply"].startswith(
        "That analysis took"
    )
    st = fake.start_turn(A, CHAT, "again", "7d", "all", False, ("u2", "r2"))
    monkeypatch.setattr(client, "invoke", lambda p, s: (_ for _ in ()).throw(KeyError("secret")))
    m["advisor.worker"].handler({"owner": A, "chat_id": CHAT, "reply_sk": st["reply_sk"]}, None)
    assert fake.finished[-1][3] == "failed" and "secret" not in json.dumps(fake.finished[-1][4])


def test_worker_rejects_malformed_events(m, wired):
    assert m["advisor.worker"].handler({"owner": "x y", "chat_id": CHAT, "reply_sk": "MSG#"}, None) == {
        "ok": False
    }
    assert m["advisor.worker"].handler({"owner": A, "chat_id": CHAT, "reply_sk": "CHAT#other"}, None) == {
        "ok": False
    }


# ---- runtime client ------------------------------------------------------------------------


def test_client_single_attempt_stops_session_on_timeout(m, monkeypatch):
    c = m["advisor.client"]
    stops = []

    class RT:
        def invoke_agent_runtime(self, **kw):
            raise ReadTimeoutError(endpoint_url="https://x")

        def stop_runtime_session(self, **kw):
            stops.append(kw)

    monkeypatch.setattr(c, "_client", lambda: RT())
    monkeypatch.setattr(c, "_stop_client", lambda: RT())
    with pytest.raises(c.AdvisorTimeout):
        c.invoke({"message": "q"}, "advisor-s")
    assert stops and stops[0]["runtimeSessionId"] == "advisor-s"


def test_client_retries_409_once_then_parses(m, monkeypatch):
    c = m["advisor.client"]
    calls = []

    class RT:
        def invoke_agent_runtime(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise ClientError({"Error": {"Code": "RetryableConflictException"}}, "InvokeAgentRuntime")
            return {"response": io.BytesIO(json.dumps({"outcome": "answered"}).encode())}

    monkeypatch.setattr(c, "_client", lambda: RT())
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    assert c.invoke({"message": "q"}, "advisor-s") == {"outcome": "answered"}
    assert len(calls) == 2


def test_client_error_payload_raises(m, monkeypatch):
    c = m["advisor.client"]

    class RT:
        def invoke_agent_runtime(self, **kw):
            return {"response": io.BytesIO(b'{"error": "advisor failed"}')}

    monkeypatch.setattr(c, "_client", lambda: RT())
    with pytest.raises(c.AdvisorRuntimeError):
        c.invoke({"message": "q"}, "s")


def test_client_boto_config_is_one_attempt(m):
    c = m["advisor.client"]
    cfg = c._client().meta.config
    assert cfg.retries["total_max_attempts"] == 1 and cfg.read_timeout == 170


def test_client_read_timeout_uses_a_shorter_client(m, monkeypatch):
    c = m["advisor.client"]
    cfg = c._client(95).meta.config
    assert cfg.read_timeout == 95 and cfg.retries["total_max_attempts"] == 1
    used = []

    class RT:
        def __init__(self, name):
            self.name = name

        def invoke_agent_runtime(self, **kw):
            used.append(self.name)
            return {"response": io.BytesIO(b'{"outcome": "answered"}')}

    monkeypatch.setattr(c, "_client", lambda t=None: RT(f"timed:{t}" if t else "default"))
    c.invoke({"message": "q"}, "s")
    c.invoke({"message": "q"}, "s", read_timeout=95)
    assert used == ["default", "timed:95"]


# ---- chat store (real expressions against a recording stub) -------------------------------

import re as _re

_EXPR_WORDS = {
    "SET",
    "REMOVE",
    "ADD",
    "DELETE",
    "AND",
    "OR",
    "NOT",
    "attribute_exists",
    "attribute_not_exists",
    "begins_with",
    "size",
    "list_append",
    "if_not_exists",
}


def assert_aliased(expr: str):
    """Every attribute name in an expression must be a #placeholder (reserved-word safe)."""
    for tok in _re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _re.sub(r"[#:][A-Za-z0-9_]+", " ", expr)):
        assert tok in _EXPR_WORDS, f"bare attribute name {tok!r} in {expr!r}"


class Rec:
    def __init__(self, pages=None, fail=None, get=None):
        self.calls = []
        self.pages = pages or [{"Items": []}]
        self.fail = fail or {}
        self.get = get

    def __getattr__(self, name):
        def f(**kw):
            self.calls.append((name, kw))
            for k in ("UpdateExpression", "ConditionExpression"):
                if k in kw:
                    assert_aliased(kw[k])
            if name in self.fail:
                code = self.fail[name]
                code = code.pop(0) if isinstance(code, list) else code
                if code:
                    raise ClientError({"Error": {"Code": code}}, name)
            if name == "query":
                i = sum(1 for c in self.calls if c[0] == "query") - 1
                return self.pages[min(i, len(self.pages) - 1)]
            if name == "get_item":
                return {"Item": self.get} if self.get else {}
            if name == "update_item":
                return {"Attributes": {"status": "pending", "request_text": "q"}}
            if name == "transact_write_items":
                for op in kw["TransactItems"]:
                    for v in op.values():
                        for k in ("UpdateExpression", "ConditionExpression"):
                            if k in v:
                                assert_aliased(v[k])
            return {}

        return f

    def batch_writer(self):
        rec = self

        class W:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def delete_item(self, Key):
                rec.calls.append(("delete_item", Key))

        return W()


def _store(m, monkeypatch, table=None, client=None):
    chats = m["advisor.chats"]
    monkeypatch.setattr(chats, "_table", lambda: table or Rec())
    monkeypatch.setattr(chats, "_client", lambda: client or Rec())
    monkeypatch.setattr(chats.time, "time", lambda: 1000)
    return chats


def test_store_new_chat_single_transaction(m, monkeypatch):
    t, cl = Rec(pages=[{"Items": [], "Count": 0}]), Rec()
    chats = _store(m, monkeypatch, t, cl)
    out = chats.start_turn(A, CHAT, "q" * 100, "7d", "all", True, ("u1", "r1"))
    tx = cl.calls[0][1]["TransactItems"]
    hdr = tx[0]["Put"]
    assert hdr["Item"]["pk"] == {"S": f"USER#{A}"} and hdr["Item"]["title"] == {"S": ("Q" + "q" * 99)[:40]}
    assert (
        hdr["Item"]["ttl"] == {"N": str(1000 + 90 * 86400)}
        and hdr["ConditionExpression"] == "attribute_not_exists(#sk)"
    )
    assert tx[1]["Put"]["Item"]["role"] == {"S": "user"}
    assert tx[2]["Put"]["Item"]["status"] == {"S": "pending"} and tx[2]["Put"]["Item"]["request_text"] == {
        "S": "q" * 100
    }
    assert all(op[k]["Item" if k == "Put" else "Key"]["pk"] == {"S": f"USER#{A}"} for op in tx for k in op)
    assert out["reply_sk"].startswith(f"MSG#{CHAT}#") and out["reply_sk"].endswith("#1#r1")
    assert not [c for c in t.calls if c[0] == "put_item"]  # header is inside the transaction


def test_store_existing_chat_condition_and_errors(m, monkeypatch):
    cl = Rec(fail={"transact_write_items": "TransactionCanceledException"})
    chats = _store(m, monkeypatch, Rec(get={"pk": f"USER#{A}", "sk": f"CHAT#{CHAT}", "turn_count": 3}), cl)
    with pytest.raises(chats.ChatBusy):
        chats.start_turn(A, CHAT, "q", "7d", "all", False, ("u", "r"))
    upd = cl.calls[0][1]["TransactItems"][0]["Update"]
    assert "(attribute_not_exists(#pm) OR #ps < :stale)" in upd["ConditionExpression"]
    assert "#tc < :max" in upd["ConditionExpression"]
    chats = _store(m, monkeypatch, Rec(get=None), cl)
    with pytest.raises(chats.ChatNotFound):
        chats.start_turn(A, CHAT, "q", "7d", "all", False, ("u", "r"))
    chats = _store(m, monkeypatch, Rec(get={"sk": f"CHAT#{CHAT}", "turn_count": 50}), cl)
    with pytest.raises(chats.ChatLimit):
        chats.start_turn(A, CHAT, "q", "7d", "all", False, ("u", "r"))


def test_store_no_chat_count_limit(m, monkeypatch):
    chats = _store(m, monkeypatch, Rec(pages=[{"Items": [], "Count": 500}]))
    assert chats.start_turn(A, CHAT, "q", "7d", "all", True, ("u", "r"))["chat_id"] == CHAT


def test_store_finish_reply_conditional_and_aliased(m, monkeypatch):
    t = Rec()
    chats = _store(m, monkeypatch, t)
    assert chats.finish_reply(A, CHAT, "MSG#x", "complete", {"reply": "ok", "findings": [{"value": 1.5}]})
    msg, hdr = t.calls[0][1], t.calls[1][1]
    assert msg["ConditionExpression"] == "attribute_exists(#sk) AND #s = :pending"
    assert (
        msg["ExpressionAttributeNames"]["#rq"] == "request_text" and "REMOVE #rq" in msg["UpdateExpression"]
    )
    assert (
        hdr["Key"] == {"pk": f"USER#{A}", "sk": f"CHAT#{CHAT}"} and hdr["ConditionExpression"] == "#pm = :r"
    )
    assert hdr["ExpressionAttributeValues"][":ttl"] == 1000 + 90 * 86400  # header ttl refreshed
    # chat deleted mid-answer -> dropped, never recreated
    t2 = Rec(fail={"update_item": "ConditionalCheckFailedException"})
    chats = _store(m, monkeypatch, t2)
    assert chats.finish_reply(A, CHAT, "MSG#x", "complete", {"reply": "ok"}) is False
    assert len(t2.calls) == 1


def test_store_claim_once(m, monkeypatch):
    t = Rec()
    chats = _store(m, monkeypatch, t)
    assert chats.claim_reply(A, "MSG#x")["request_text"] == "q"
    call = t.calls[-1][1]
    assert call["ConditionExpression"] == "#s = :pending AND attribute_not_exists(#ca) AND #cr > :cutoff"
    assert call["ExpressionAttributeNames"]["#cr"] == "created_at"
    chats = _store(m, monkeypatch, Rec(fail={"update_item": "ConditionalCheckFailedException"}))
    assert chats.claim_reply(A, "MSG#x") is None
    # already claimed (or finished) -> duplicate, even when old
    old = "2000-01-01T00:00:00.000000+00:00"
    chats = _store(
        m,
        monkeypatch,
        Rec(
            fail={"update_item": "ConditionalCheckFailedException"},
            get={"status": "pending", "created_at": old, "claimed_at": old},
        ),
    )
    assert chats.claim_reply(A, "MSG#x") is None


def test_store_claim_refuses_an_expired_reply(m, monkeypatch):
    chats = m["advisor.chats"]
    assert chats.CLAIM_MAX_AGE_SECONDS + 180 < chats.STALE_PENDING_SECONDS  # worker timeout fits before stale
    t = Rec(
        fail={"update_item": "ConditionalCheckFailedException"},
        get={"status": "pending", "created_at": "2000-01-01T00:00:00.000000+00:00"},
    )
    chats = _store(m, monkeypatch, t)
    with pytest.raises(chats.ReplyExpired):
        chats.claim_reply(A, "MSG#x")
    get = [c[1] for c in t.calls if c[0] == "get_item"][0]
    assert get["ConsistentRead"] is True and get["Key"] == {"pk": f"USER#{A}", "sk": "MSG#x"}
    # a fresh unclaimed reply that failed the condition for another reason is not "expired"
    fresh = chats._iso(chats._now_dt())
    chats = _store(
        m,
        monkeypatch,
        Rec(
            fail={"update_item": "ConditionalCheckFailedException"},
            get={"status": "pending", "created_at": fresh},
        ),
    )
    assert chats.claim_reply(A, "MSG#x") is None


def test_store_limits_in_flight_answers_per_owner(m, monkeypatch):
    chats = m["advisor.chats"]
    fresh, old = chats._iso(chats._now_dt()), "2000-01-01T00:00:00.000000+00:00"

    def hdr(i, since):
        return {"pk": f"USER#{A}", "sk": f"CHAT#c{i}", "pending_msg": f"MSG#c{i}#x", "pending_since": since}

    busy = [hdr(i, fresh) for i in range(3)]
    cl = Rec()
    chats = _store(m, monkeypatch, Rec(pages=[{"Items": busy}]), cl)
    with pytest.raises(chats.ChatLimit, match="3 answers in progress"):
        chats.start_turn(A, CHAT, "q", "7d", "all", True, ("u", "r"))
    assert cl.calls == []  # refused before the transaction
    # stale pendings and headers without a pending reply do not count
    items = busy[:2] + [hdr(3, old), {"pk": f"USER#{A}", "sk": "CHAT#c4"}]
    chats = _store(m, monkeypatch, Rec(pages=[{"Items": items}]), Rec())
    assert chats.start_turn(A, CHAT, "q", "7d", "all", True, ("u", "r"))["chat_id"] == CHAT


def test_api_in_flight_limit_is_400(m, wired, monkeypatch):
    chats = m["advisor.api"].chats

    def limited(*a, **kw):
        raise chats.ChatLimit("You have 3 answers in progress; wait for one to finish.")

    monkeypatch.setattr(chats, "start_turn", limited)
    code, err = api(m, "POST", "/advisor/chats", {"message": "q"})
    assert code == 400 and "answers in progress" in err["error"]


def test_store_delete_all_partition_query_and_pagination(m, monkeypatch):
    items = [{"pk": f"USER#{A}", "sk": f"MSG#{CHAT}#{i:03d}"} for i in range(30)] + [
        {"pk": f"USER#{A}", "sk": f"CHAT#{CHAT}"}
    ]
    pages = [{"Items": items[:20], "LastEvaluatedKey": {"pk": "x", "sk": "y"}}, {"Items": items[20:]}]
    t = Rec(pages=pages)
    chats = _store(m, monkeypatch, t)
    assert chats.delete_all(A) == 1
    queries = [c[1] for c in t.calls if c[0] == "query"]
    assert len(queries) == 2 and queries[1]["ExclusiveStartKey"] == {"pk": "x", "sk": "y"}
    cond = queries[0]["KeyConditionExpression"].get_expression()
    assert cond["operator"] == "=" and cond["values"][1] == f"USER#{A}"  # pk only: no empty begins_with
    assert len([c for c in t.calls if c[0] == "delete_item"]) == 31


def test_store_stale_pending_shown_as_failed(m, monkeypatch):
    chats = m["advisor.chats"]
    old = "2000-01-01T00:00:00.000000+00:00"
    assert chats._public_msg({"status": "pending", "created_at": old, "text": ""})["status"] == "failed"
    fresh = chats._iso(chats._now_dt())
    assert chats._public_msg({"status": "pending", "created_at": fresh, "text": ""})["status"] == "pending"


def test_worker_always_reaches_terminal_state(m, wired, monkeypatch):
    fake, _ = wired
    ev = _start(m, fake)
    monkeypatch.setattr(
        m["advisor.worker"].chats, "messages", lambda o, c: (_ for _ in ()).throw(RuntimeError("throttled"))
    )
    assert m["advisor.worker"].handler(ev, None) == {"ok": False}
    assert fake.finished[-1][3] == "failed"
    assert "pending_msg" not in fake.db[A][f"CHAT#{CHAT}"]


def test_worker_sets_agent_title_on_first_turn_only(m, wired, monkeypatch):
    fake, _ = wired
    titles = []
    monkeypatch.setattr(
        m["advisor.worker"].chats, "set_title", lambda o, c, t: titles.append(t), raising=False
    )
    ev = _start(m, fake)
    monkeypatch.setattr(
        m["advisor.worker"].advisor_client,
        "invoke",
        lambda p, s: {**_answer("Total cost is [[t1.total.cost]]."), "title": "Agent cost 9 breakdown!"},
    )
    m["advisor.worker"].handler(ev, None)
    assert titles == ["Agent cost breakdown"]
    st = fake.start_turn(A, CHAT, "and the order agent?", "7d", "all", False, ("u2", "r2"))
    m["advisor.worker"].handler({"owner": A, "chat_id": CHAT, "reply_sk": st["reply_sk"]}, None)
    assert titles == ["Agent cost breakdown"]  # follow-ups never retitle


def test_store_set_title(m, monkeypatch):
    t = Rec()
    chats = _store(m, monkeypatch, t)
    chats.set_title(A, CHAT, "Agent cost breakdown")
    call = t.calls[-1][1]
    assert (
        call["Key"] == {"pk": f"USER#{A}", "sk": f"CHAT#{CHAT}"}
        and call["ExpressionAttributeValues"][":t"] == "Agent cost breakdown"
    )
    assert call["ConditionExpression"] == "attribute_exists(#sk)"


# ---- deploy-time timing and capacity (read from the stack source; no CDK synth) -----------


def _worker_block() -> str:
    src = (_ROOT / "infra" / "stacks" / "control_plane_stack.py").read_text()
    start = src.index('"AdvisorWorker"')
    return src[start : src.index("configure_async_invoke(", start) + 200]


def test_worker_queue_age_plus_timeout_ends_before_stale(m):
    block = _worker_block()
    timeout = int(_re.search(r"timeout=Duration\.seconds\((\d+)\)", block).group(1))
    age = int(_re.search(r"max_event_age=Duration\.seconds\((\d+)\)", block).group(1))
    assert age + timeout < m["advisor.chats"].STALE_PENDING_SECONDS
    assert m["advisor.chats"].CLAIM_MAX_AGE_SECONDS + timeout < m["advisor.chats"].STALE_PENDING_SECONDS


def test_runtime_runs_the_data_tools_in_process():
    src = (_ROOT / "infra" / "stacks" / "control_plane_stack.py").read_text()
    # Names split so a repo-wide grep for the removed tools function stays empty.
    for gone in ("Advisor" + "Tools", "ADVISOR_" + "TOOLS_FUNCTION", "tools" + "_fn"):
        assert gone not in src
    start = src.index('"AdvisorAgentRuntime"')
    runtime = src[start : src.index('"AdvisorWorker"', start)]
    for table in ("self.metrics_table", "data.usage_plans_table", "data.flags_table"):
        assert f"{table}.grant_read_data(self.advisor_runtime.role)" in runtime
    assert '"cognito-idp:AdminGetUser"' in runtime and '"bedrock:Retrieve"' in runtime


def test_advisor_image_copies_only_allowlisted_files():
    src = (_ROOT / "infra" / "stacks" / "control_plane_stack.py").read_text()
    block = src[src.index("_ADVISOR_IMAGE_FILES = (") :]
    allowed = _re.findall(r'"([^"]+)"', block[: block.index(")")])
    dockerfile = (_ROOT / "src" / "control-plane" / "advisor" / "agent" / "Dockerfile").read_text()
    sources = _re.findall(r"^COPY\s+(\S+)\s", dockerfile, _re.M)
    assert sources and all((_ROOT / a).exists() for a in allowed)
    # A COPY source is an allowlisted path, inside one, or a directory holding some
    # (the build context then carries only the allowlisted files).
    for source in sources:
        assert any(
            source == a or source.startswith(a + "/") or a.startswith(source + "/") for a in allowed
        ), source
        assert (_ROOT / source).exists(), source
