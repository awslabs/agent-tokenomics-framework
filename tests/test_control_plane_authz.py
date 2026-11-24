"""Unit tests for control-plane roles and the governance audit trail (threat model
M14/M16, T8). No AWS: the handlers and the audit table are faked.

governance-admin may change flags / usage plans / overrides and start benchmarks,
and each change is audited with who + before/after. viewer is read-only. A
signed-in admin-pool user in neither group gets 403 everywhere.
"""

from __future__ import annotations

import json

import pytest

ADMIN = {"sub": "sub-admin", "email": "admin@example.com", "cognito:groups": "governance-admin"}
VIEWER = {"sub": "sub-viewer", "email": "viewer@example.com", "cognito:groups": "viewer"}
NOBODY = {"sub": "sub-nobody", "email": "nobody@example.com"}

WRITES = [
    ("PUT", "/flags", {"memory": {"enabled": True}}),
    ("PUT", "/governance/config", {"session_max_tokens": 1}),
    ("PUT", "/governance/user-override", {"email": "bob@example.com", "quota": {}}),
    ("POST", "/benchmarks", {"name": "x"}),
]
READS = ["/flags", "/governance/config", "/governance/audit", "/benchmarks", "/me"]


@pytest.fixture
def cp(monkeypatch, cp_import):
    entry, authz, audit, flags_h, gov_h, bench_h = cp_import(
        "lambda_entry",
        "lib.authz",
        "lib.audit",
        "handlers.flags",
        "handlers.governance",
        "handlers.benchmarks",
    )
    state = {"flags": {"memory": {"enabled": False}}, "gov": {"session_max_tokens": 0}, "audit": []}

    def put_flags(body):
        state["flags"] = {**state["flags"], **body}
        return state["flags"]

    def put_config(body):
        state["gov"] = {**state["gov"], **body}
        return state["gov"]

    monkeypatch.setattr(flags_h, "get_flags", lambda: dict(state["flags"]))
    monkeypatch.setattr(flags_h, "put_flags", put_flags)
    monkeypatch.setattr(gov_h, "get_config", lambda: dict(state["gov"]))
    monkeypatch.setattr(gov_h, "put_config", put_config)
    monkeypatch.setattr(gov_h, "put_user_override", put_config)
    monkeypatch.setattr(bench_h, "start_benchmark", lambda body: {"benchmark_id": "bm-1", **body})
    monkeypatch.setattr(bench_h, "list_benchmarks", lambda: [])

    class FakeTable:
        def put_item(self, Item, ConditionExpression):  # boto3 kwargs
            state["audit"].append(Item)

        def query(self, **_):
            return {"Items": list(reversed(state["audit"]))}

    monkeypatch.setattr(audit, "_table", lambda: FakeTable())
    return entry, authz, state


def call(entry, method, path, claims, body=None):
    ev = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": claims}},
    }
    r = entry.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("governance-admin", {"governance-admin"}),
        ("[governance-admin, viewer]", {"governance-admin", "viewer"}),
        ("[governance-admin viewer]", {"governance-admin", "viewer"}),
        (["viewer"], {"viewer"}),
        ("", set()),
        (None, set()),
    ],
)
def test_group_claim_parsing(cp, raw, expected):
    _, authz, _ = cp
    assert authz.groups({"cognito:groups": raw}) == expected


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_viewer_cannot_change_anything(cp, method, path, body):
    entry, _, state = cp
    before = json.dumps(state, sort_keys=True)
    status, resp = call(entry, method, path, VIEWER, body)
    assert status == 403 and resp["error"] == "forbidden"
    assert json.dumps(state, sort_keys=True) == before  # nothing changed, nothing audited


@pytest.mark.parametrize("path", READS)
def test_viewer_can_read(cp, path):
    entry, _, _ = cp
    assert call(entry, "GET", path, VIEWER)[0] == 200


@pytest.mark.parametrize(("method", "path"), [("GET", p) for p in READS] + [(m, p) for m, p, _ in WRITES])
def test_no_role_gets_nothing(cp, method, path):
    entry, _, _ = cp
    assert call(entry, method, path, NOBODY, {})[0] == 403


def test_admin_change_is_applied_and_audited(cp):
    entry, _, state = cp
    status, resp = call(entry, "PUT", "/flags", ADMIN, {"memory": {"enabled": True}})
    assert status == 200 and resp["memory"]["enabled"] is True
    (rec,) = state["audit"]
    assert rec["action"] == "PUT /flags"
    assert (rec["actor_sub"], rec["actor_email"]) == ("sub-admin", "admin@example.com")
    assert rec["before"] == {"memory": {"enabled": False}}
    assert rec["after"] == {"memory": {"enabled": True}}


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_every_admin_write_is_audited(cp, method, path, body):
    entry, _, state = cp
    assert call(entry, method, path, ADMIN, body)[0] in (200, 202)
    assert [r["action"] for r in state["audit"]] == [f"{method} {path}"]


def test_audit_listing_is_newest_first(cp):
    entry, _, _ = cp
    call(entry, "PUT", "/governance/config", ADMIN, {"session_max_tokens": 1})
    call(entry, "PUT", "/governance/config", ADMIN, {"session_max_tokens": 2})
    _, resp = call(entry, "GET", "/governance/audit", VIEWER)
    assert [e["after"]["session_max_tokens"] for e in resp["entries"]] == [2, 1]


def test_me_reports_write_permission(cp):
    entry, _, _ = cp
    assert call(entry, "GET", "/me", ADMIN)[1] == {"groups": ["governance-admin"], "can_write": True}
    assert call(entry, "GET", "/me", VIEWER)[1] == {"groups": ["viewer"], "can_write": False}


def test_internal_errors_are_generic(cp, monkeypatch):
    entry, _, _ = cp

    def boom():
        raise RuntimeError("arn:aws:dynamodb:us-east-1:111122223333:table/secret")

    monkeypatch.setattr(entry.flags_handler, "get_flags", boom)
    status, resp = call(entry, "GET", "/flags", VIEWER)
    assert status == 500 and resp == {"error": "internal error"}
