"""/advisor/chats handlers -- the Tokenomics Advisor API (admin-only, read-only advice).

  POST   /advisor/chats             {message, window?, user?, chat_id?} -> 202 {chat_id, message_id}
  GET    /advisor/chats             -> {chats: [...]}            (caller's chats, newest first)
  GET    /advisor/chats/{chat_id}   -> {chat_id, title, messages: [...]}   (poll until not pending)
  DELETE /advisor/chats/{chat_id}   -> {deleted: chat_id}
  DELETE /advisor/chats             -> {deleted: <count>}        (all of the caller's chats)

The owner is the Cognito `sub` from the verified authorizer claims, never the
body, and every store call is keyed by it. POST stores the admin's message plus a
pending reply and async-invokes the AdvisorWorker (the same pattern as
POST /benchmarks), so no request waits on the agent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache, wraps
from typing import Any

import boto3

import config
from advisor import chats, grounding

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _lambda_client():
    return boto3.client("lambda", region_name=config.AWS_REGION)


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Expected outcomes pass through (404/409/403/400); anything else becomes a
    generic error so storage/AWS details (table names, ARNs) never reach the client."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (chats.ChatNotFound, chats.ChatBusy, PermissionError, ValueError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[advisor] %s failed (%s)", fn.__name__, type(exc).__name__)
            raise RuntimeError("advisor request failed") from None

    return wrapper


def owner_of(claims: dict[str, Any] | None) -> str:
    sub = str((claims or {}).get("sub") or "")
    if not grounding._SUB_RE.match(sub):
        raise PermissionError("missing caller identity")
    return sub


def _chat_id(chat_id: str) -> str:
    if not grounding._UUID_RE.match(chat_id or ""):
        raise chats.ChatNotFound(chat_id)
    return chat_id


@_safe
def send(body: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    owner = owner_of(claims)
    req = grounding.validate_body(body)
    new_chat = req["chat_id"] is None
    chat_id = req["chat_id"] or grounding.new_id()
    started = chats.start_turn(
        owner,
        chat_id,
        req["message"],
        req["window"],
        req["user"],
        new_chat,
        (grounding.new_id(), grounding.new_id()),
    )
    payload = {"owner": owner, "chat_id": chat_id, "reply_sk": started["reply_sk"]}
    try:
        _lambda_client().invoke(
            FunctionName=config.ADVISOR_WORKER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 -- never leave the reply stuck pending
        logger.warning("[advisor] worker invoke failed (%s)", type(exc).__name__)
        chats.finish_reply(owner, chat_id, started["reply_sk"], "failed", {"reply": grounding.NOTE_FAILED})
    return {"chat_id": chat_id, "message_id": started["message_id"], "status": "pending"}


@_safe
def list_chats(claims: dict[str, Any]) -> dict[str, Any]:
    return {"chats": chats.list_chats(owner_of(claims))}


@_safe
def get_chat(chat_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    return chats.get_chat(owner_of(claims), _chat_id(chat_id))


@_safe
def delete_chat(chat_id: str, claims: dict[str, Any]) -> dict[str, Any]:
    chats.delete_chat(owner_of(claims), _chat_id(chat_id))
    return {"deleted": chat_id}


@_safe
def delete_all(claims: dict[str, Any]) -> dict[str, Any]:
    return {"deleted": chats.delete_all(owner_of(claims))}
