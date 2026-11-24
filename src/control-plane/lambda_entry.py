"""API Gateway (proxy) -> control-plane handlers adapter.

The Lambda behind the REST API is a proxy integration: every request arrives as
one event and this module routes it to the same handlers the design specifies.
The route table maps 1:1 to the architecture's API surface:

  GET    /health
  GET    /flags                       PUT /flags
  GET    /governance/config           PUT /governance/config
  GET    /governance/analytics        PUT /governance/user-override
  GET    /governance/events           GET /governance/audit
  POST   /benchmarks                  GET /benchmarks
  GET    /benchmarks/{benchmark_id}
  GET    /advisor/chats               POST /advisor/chats      DELETE /advisor/chats
  GET    /advisor/chats/{chat_id}     DELETE /advisor/chats/{chat_id}

(Customer chat is NOT here: the chat UI calls the agent runtime directly. This
API is for admins only -- flags + benchmarks -- and is protected by a Cognito
authorizer on the dedicated control-plane user pool.)

Authorization (threat model M14/M16, lib/authz.py + lib/audit.py): every route
needs the `viewer` or `governance-admin` group; changes (PUT routes and
POST /benchmarks) need `governance-admin` and each one writes an audit record
of who changed what, before and after.

Benchmark note: `POST /benchmarks` creates one benchmark record and async-invokes
a dedicated worker Lambda (InvocationType="Event"), returning the record
immediately (202). The worker runs Run A then Run B to completion in its own
execution environment and writes progress/results back into the single record;
the UI polls GET /benchmarks/{benchmark_id}.
"""

from __future__ import annotations

# --- import bootstrap: make `handlers`, `lib`, `config`, `shared` importable ---
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
for p in (_HERE, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)
# ------------------------------------------------------------------------------

import json
import logging
from collections.abc import Callable
from typing import Any

from advisor import api as advisor_handler
from advisor.chats import ChatBusy, ChatNotFound
from handlers import benchmarks as benchmarks_handler
from handlers import flags as flags_handler
from handlers import governance as governance_handler
from lib import audit, authz
from shared.logging_setup import configure_logging

configure_logging()  # Lambda installs a root handler; set our level on it so INFO logs show
logger = logging.getLogger(__name__)

_CORS = {
    # Scoped to the admin SPA origin (set by the stack); "*" only as a dev fallback.
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
}


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_CORS},
        "body": json.dumps(body),
    }


def _path(event: dict[str, Any]) -> str:
    # REST API (v1) uses "path"; HTTP API (v2) uses rawPath. Support both.
    raw = event.get("path") or event.get("rawPath") or "/"
    return raw.rstrip("/") or "/"


def _method(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    return (event.get("httpMethod") or http.get("method") or "GET").upper()


def _query(event: dict[str, Any]) -> dict[str, str]:
    return event.get("queryStringParameters") or {}


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    """Verified Cognito claims from the API Gateway authorizer (never the body)."""
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _change(
    claims: dict[str, Any],
    action: str,
    apply: Callable[[], Any],
    before: Callable[[], Any] | None = None,
) -> Any:
    """Apply a governance change as a governance-admin, then audit it."""
    authz.require_write(claims)
    prior = before() if before else None
    result = apply()
    audit.record(claims, action, before=prior, after=result)
    return result


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = _method(event)
    path = _path(event)

    try:
        if method == "OPTIONS":
            return _resp(204, {})

        claims = _claims(event)
        authz.require_read(claims)

        if method == "GET":
            if path == "/health":
                return _resp(200, {"status": "ok", "agent_arn_set": bool(_agent_arn())})
            if path == "/flags":
                return _resp(200, flags_handler.get_flags())
            if path == "/governance/config":
                return _resp(200, governance_handler.get_config())
            if path == "/governance/analytics":
                q = _query(event)
                return _resp(200, governance_handler.analytics(q.get("user", "all"), q.get("window", "7d")))
            if path == "/governance/events":
                q = _query(event)
                return _resp(
                    200,
                    governance_handler.events(q.get("user", "all"), q.get("window", "7d"), q.get("config")),
                )
            if path == "/governance/audit":
                return _resp(200, {"entries": audit.list_entries(int(_query(event).get("limit", 100)))})
            if path == "/me":
                return _resp(
                    200, {"groups": sorted(authz.groups(claims)), "can_write": authz.can_write(claims)}
                )
            if path == "/benchmarks":
                return _resp(200, {"runs": benchmarks_handler.list_benchmarks()})
            if path.startswith("/benchmarks/"):
                benchmark_id = path.rsplit("/", 1)[-1]
                bm = benchmarks_handler.get_benchmark(benchmark_id)
                return _resp(200 if bm else 404, bm or {"error": "benchmark not found"})
            if path == "/advisor/chats":
                return _resp(200, advisor_handler.list_chats(_claims(event)))
            if path.startswith("/advisor/chats/"):
                return _resp(200, advisor_handler.get_chat(path.rsplit("/", 1)[-1], _claims(event)))
            return _resp(404, {"error": "not found"})

        if method == "PUT":
            body = _body(event)
            if path == "/flags":
                return _resp(
                    200,
                    _change(
                        claims,
                        "PUT /flags",
                        lambda: flags_handler.put_flags(body),
                        before=flags_handler.get_flags,
                    ),
                )
            if path == "/governance/config":
                return _resp(
                    200,
                    _change(
                        claims,
                        "PUT /governance/config",
                        lambda: governance_handler.put_config(body),
                        before=governance_handler.get_config,
                    ),
                )
            if path == "/governance/user-override":
                return _resp(
                    200,
                    _change(
                        claims,
                        "PUT /governance/user-override",
                        lambda: governance_handler.put_user_override(body),
                        before=governance_handler.get_config,
                    ),
                )
            return _resp(404, {"error": "not found"})

        if method == "POST":
            if path == "/benchmarks":
                body = _body(event)
                return _resp(
                    202,
                    _change(claims, "POST /benchmarks", lambda: benchmarks_handler.start_benchmark(body)),
                )
            if path == "/advisor/chats":
                return _resp(202, advisor_handler.send(_body(event), _claims(event)))
            return _resp(404, {"error": "not found"})

        if method == "DELETE":
            if path == "/advisor/chats":
                return _resp(200, advisor_handler.delete_all(_claims(event)))
            if path.startswith("/advisor/chats/"):
                return _resp(200, advisor_handler.delete_chat(path.rsplit("/", 1)[-1], _claims(event)))
            return _resp(404, {"error": "not found"})

        return _resp(405, {"error": f"method {method} not allowed"})
    except ChatNotFound:
        return _resp(404, {"error": "chat not found"})
    except ChatBusy:
        return _resp(409, {"error": "the Advisor is still answering the previous message"})
    except PermissionError as e:
        return _resp(403, {"error": "forbidden", "detail": str(e)})
    except ValueError as e:
        return _resp(400, {"error": str(e)})
    except Exception:  # details to CloudWatch, not the client (M15)
        logger.exception("[control-plane-api] %s %s failed", method, path)
        return _resp(500, {"error": "internal error"})


def _agent_arn() -> str:
    import config

    return config.AGENT_RUNTIME_ARN
