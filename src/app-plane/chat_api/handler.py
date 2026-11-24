"""App-plane chat API Lambda -- the chat UI's entry to the agent.

Thin, app-plane-owned proxy behind an API Gateway Cognito authorizer. The chat
SPA logs in via Cognito and sends the JWT; API Gateway validates it and injects
the user's claims. This Lambda:
  - reads identity from the VERIFIED JWT claims (never from the request body),
  - builds a stable AgentCore session id from the user + their conversation id,
  - forwards { message, id_token, session_id } to the agent runtime, which
    re-verifies the token itself and takes user_id + profile from its claims
    (threat model M8: the runtime trusts no caller-asserted identity).

Route (API Gateway proxy, Cognito-authorized):
  POST /chat  { "message": str, "conversation_id"?: str }
  Authorization: Bearer <Cognito id token>
  -> { "reply", "usage", "identity", "model_ids" }

Identity comes from claims: `sub` (user_id), `email`, `custom:tier`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_QUALIFIER = os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT")
_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT_SECONDS", "120"))
# Input limits (threat model T6): keep in step with the runtime's MAX_MESSAGE_CHARS.
_MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "4000"))
_MAX_CONVERSATION_ID_CHARS = 64

logger = logging.getLogger(__name__)

_CORS = {
    # Scoped to the chat SPA origin (set by the stack); "*" only as a dev fallback.
    "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "bedrock-agentcore",
        region_name=_REGION,
        config=BotoConfig(read_timeout=_TIMEOUT, retries={"max_attempts": 2}),
    )


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_CORS},
        "body": json.dumps(body),
    }


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    """The verified Cognito claims injected by the API Gateway authorizer."""
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def _bearer_token(event: dict[str, Any]) -> str:
    """The raw id token the authorizer already verified (forwarded for the runtime)."""
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    value = str(headers.get("authorization") or "").strip()
    return value[7:].strip() if value[:7].lower() == "bearer " else value


def _session_id(user_id: str, conversation_id: str) -> str:
    """Stable AgentCore runtimeSessionId for this user's conversation.

    Same user + same conversation -> same session (turns grouped, prompt-cache
    reuse). AgentCore requires 33-128 chars; we sanitize and pad.
    """
    raw = f"{user_id}-{conversation_id}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", raw)
    if len(safe) < 33:
        safe = f"{safe}-{uuid.uuid4().hex}"
    return safe[:128]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        event.get("httpMethod")
        or (event.get("requestContext", {}).get("http", {}) or {}).get("method")
        or "POST"
    ).upper()
    if method == "OPTIONS":
        return _resp(204, {})

    if not _RUNTIME_ARN:
        return _resp(500, {"error": "AGENT_RUNTIME_ARN not configured"})

    claims = _claims(event)
    user_id = claims.get("sub")
    if not user_id:
        # No verified identity -> the authorizer should have blocked this.
        return _resp(401, {"error": "unauthorized"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})

    message = (body.get("message") or "").strip()
    if not message:
        return _resp(400, {"error": "message is required"})
    if len(message) > _MAX_MESSAGE_CHARS:
        return _resp(400, {"error": f"Message is too long (max {_MAX_MESSAGE_CHARS} characters)."})

    id_token = _bearer_token(event)
    if not id_token:
        return _resp(401, {"error": "unauthorized"})

    conversation_id = (body.get("conversation_id") or "default").strip() or "default"
    if len(conversation_id) > _MAX_CONVERSATION_ID_CHARS:
        return _resp(400, {"error": "conversation_id is too long"})
    payload = {
        "message": message,
        "id_token": id_token,
        "session_id": _session_id(user_id, conversation_id),
    }

    try:
        resp = _client().invoke_agent_runtime(
            agentRuntimeArn=_RUNTIME_ARN,
            runtimeSessionId=payload["session_id"],
            qualifier=_QUALIFIER,
            payload=json.dumps(payload).encode("utf-8"),
        )
        data = json.loads(resp["response"].read())
    except Exception:  # details to CloudWatch, not the client
        logger.exception("[chat-api] agent invoke failed")
        return _resp(502, {"error": "agent invoke failed"})

    # The agent runtime always answers 200; a failed turn carries a non-empty
    # `error` (entrypoint._error). Surface it as a 5xx so the chat UI shows the
    # failure instead of rendering an empty reply.
    if isinstance(data, dict) and data.get("error"):
        return _resp(502, {"error": f"agent error: {data['error']}"})

    # Token governance: if the agent surfaced an in-loop per-user quota / TPM
    # throttle block (QuotaHook; see ADR-001) as a structured outcome, translate
    # it to the HTTP status the chat SPA expects (403 for quota / 429 for throttle)
    # so the UI shows a friendly notice instead of a generic error. In-loop soft
    # caps (stopped/limited on turn/agent scope) still return 200 with the reply
    # + a governance note.
    gov = (data or {}).get("governance") or {}
    reason = gov.get("reason")
    if reason == "quota":
        # monthly vs daily from the config name that fired; default daily.
        limit_hit = "monthly" if gov.get("config") == "monthly_max_tokens_per_user" else "daily"
        return _resp(
            403,
            {
                "error": "BUDGET_EXCEEDED",
                "limit_hit": limit_hit,
                "message": gov.get("detail") or "You've reached your usage limit.",
            },
        )
    if reason == "throttle":
        # Per-minute window: it clears at the top of the next minute (~60s).
        return _resp(
            429,
            {
                "retryAfter": 60,
                "message": gov.get("detail") or "You're sending requests too fast. Please wait a moment.",
            },
        )

    return _resp(200, data)
