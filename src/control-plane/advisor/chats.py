"""AdvisorChats store: per-admin Advisor conversations (and their reply jobs).

Table (ControlPlaneStack): pk / sk strings, TTL attribute `ttl`.
    pk = USER#<admin sub>                       (from the verified JWT, never the body)
    sk = CHAT#<chat_id>                         header {title, window, created_at, updated_at,
                                                        turn_count, pending_msg?, pending_since?}
    sk = MSG#<chat_id>#<ts>#<n>#<msg_id>        {role, text, status, findings, citations, ...}

Every read/delete is a Query or key operation on the caller's own pk, so one
admin can never reach another admin's chats. A pending assistant message IS the
reply job: the API creates it, the AdvisorWorker claims and completes it. Every
item is written with ttl = now + ADVISOR_CHAT_TTL_DAYS; the header's ttl is
refreshed on each activity. All attribute names in expressions are aliased
(several -- status, text, window, ttl -- are DynamoDB reserved words).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

import config
from advisor import MAX_TURNS_PER_CHAT
from advisor.grounding import NOTE_FAILED
from advisor.grounding import short_title as _short_title
from lib.store import _from_dynamo, _to_dynamo

TITLE_MAX = 40
# A reply still pending after this long is abandoned (worker never ran or died);
# the chat is unlocked and the reply is shown as failed.
STALE_PENDING_SECONDS = 300
# The worker refuses to start a reply older than this, so its 180 s timeout (plus a
# margin) still ends before STALE_PENDING_SECONDS: 300 - 180 - 20.
CLAIM_MAX_AGE_SECONDS = 100
# In-flight replies one admin may have across all chats (the worker runs 3 at once).
MAX_PENDING_PER_OWNER = 3


class ChatNotFound(LookupError):
    """No such chat for this owner (also returned for another admin's chat)."""


class ChatBusy(RuntimeError):
    """The chat already has a reply in progress."""


class ChatLimit(ValueError):
    """Per-chat or per-admin limit reached (=> 400 with a clear message)."""


class ReplyExpired(RuntimeError):
    """The pending reply waited too long to be claimed; the worker fails it unrun."""


@lru_cache(maxsize=1)
def _table():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.ADVISOR_CHATS_TABLE)


@lru_cache(maxsize=1)
def _client():
    return boto3.client("dynamodb", region_name=config.AWS_REGION)


def _pk(owner: str) -> str:
    return f"USER#{owner}"


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _ttl() -> int:
    return int(time.time()) + int(config.ADVISOR_CHAT_TTL_DAYS) * 86400


def _ser(d: dict[str, Any]) -> dict[str, Any]:
    s = TypeSerializer()
    return {k: s.serialize(_to_dynamo(v)) for k, v in d.items()}


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _query(owner: str, prefix: str = "") -> list[dict[str, Any]]:
    cond = Key("pk").eq(_pk(owner))
    if prefix:  # DynamoDB rejects an empty begins_with value on a key attribute
        cond = cond & Key("sk").begins_with(prefix)
    kwargs: dict[str, Any] = {"KeyConditionExpression": cond}
    items: list[dict[str, Any]] = []
    while True:
        resp = _table().query(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return [_from_dynamo(i) for i in items]
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _header(owner: str, chat_id: str) -> dict[str, Any] | None:
    item = _table().get_item(Key={"pk": _pk(owner), "sk": f"CHAT#{chat_id}"}).get("Item")
    return _from_dynamo(item) if item else None


def _is_stale(m: dict[str, Any]) -> bool:
    try:
        created = datetime.fromisoformat(str(m.get("created_at")))
    except ValueError:
        return False
    return _now_dt() - created > timedelta(seconds=STALE_PENDING_SECONDS)


# ---- reads ------------------------------------------------------------------


def list_chats(owner: str) -> list[dict[str, Any]]:
    chats = [
        {
            "chat_id": c["sk"].split("#", 1)[1],
            "title": c.get("title", ""),
            "window": c.get("window", "7d"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "pending": bool(c.get("pending_msg")),
        }
        for c in _query(owner, "CHAT#")
    ]
    chats.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return chats


def _public_msg(m: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "msg_id",
        "role",
        "text",
        "status",
        "window",
        "created_at",
        "findings",
        "citations",
        "citation_details",
        "notes",
    )
    out = {k: m[k] for k in keep if k in m}
    if out.get("status") == "pending" and _is_stale(m):
        out.update(status="failed", text=NOTE_FAILED)
    return out


def messages(owner: str, chat_id: str) -> list[dict[str, Any]]:
    return sorted(_query(owner, f"MSG#{chat_id}#"), key=lambda m: m["sk"])


def get_chat(owner: str, chat_id: str) -> dict[str, Any]:
    header = _header(owner, chat_id)
    if not header:
        raise ChatNotFound(chat_id)
    return {
        "chat_id": chat_id,
        "title": header.get("title", ""),
        "window": header.get("window", "7d"),
        "created_at": header.get("created_at"),
        "updated_at": header.get("updated_at"),
        "messages": [_public_msg(m) for m in messages(owner, chat_id)],
    }


# ---- writes -----------------------------------------------------------------


def start_turn(
    owner: str, chat_id: str, message: str, window: str, user: str, new_chat: bool, msg_ids: tuple[str, str]
) -> dict[str, Any]:
    """Append the admin's message + a pending assistant reply (the job), atomically.

    Raises ChatNotFound (unknown chat), ChatBusy (a reply is already pending) or
    ChatLimit (too many turns in this chat, or too many answers in progress)."""
    now_dt = _now_dt()
    now, stale = _iso(now_dt), _iso(now_dt - timedelta(seconds=STALE_PENDING_SECONDS))
    in_flight = sum(
        1
        for c in _query(owner, "CHAT#")
        if c["sk"] != f"CHAT#{chat_id}"
        and c.get("pending_msg")
        and str(c.get("pending_since") or "") >= stale
    )
    if in_flight >= MAX_PENDING_PER_OWNER:
        raise ChatLimit(f"You have {MAX_PENDING_PER_OWNER} answers in progress; wait for one to finish.")
    ttl = _ttl()
    pk = _pk(owner)
    user_id, reply_id = msg_ids
    user_sk = f"MSG#{chat_id}#{now}#0#{user_id}"
    reply_sk = f"MSG#{chat_id}#{now}#1#{reply_id}"
    table = config.ADVISOR_CHATS_TABLE
    header_key = {"pk": pk, "sk": f"CHAT#{chat_id}"}
    if new_chat:
        header_op = {
            "Put": {
                "TableName": table,
                "Item": _ser(
                    {
                        **header_key,
                        "title": _short_title(message)[:TITLE_MAX],
                        "window": window,
                        "created_at": now,
                        "updated_at": now,
                        "turn_count": 1,
                        "pending_msg": reply_sk,
                        "pending_since": now,
                        "ttl": ttl,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(#sk)",
                "ExpressionAttributeNames": {"#sk": "sk"},
            }
        }
    else:
        header_op = {
            "Update": {
                "TableName": table,
                "Key": _ser(header_key),
                "UpdateExpression": (
                    "SET #pm = :r, #ps = :now, #ua = :now, #w = :w, #ttl = :ttl ADD #tc :one"
                ),
                "ConditionExpression": (
                    "attribute_exists(#sk) AND "
                    "(attribute_not_exists(#pm) OR #ps < :stale) AND "
                    "(attribute_not_exists(#tc) OR #tc < :max)"
                ),
                "ExpressionAttributeNames": {
                    "#sk": "sk",
                    "#pm": "pending_msg",
                    "#ps": "pending_since",
                    "#ua": "updated_at",
                    "#w": "window",
                    "#ttl": "ttl",
                    "#tc": "turn_count",
                },
                "ExpressionAttributeValues": _ser(
                    {
                        ":r": reply_sk,
                        ":now": now,
                        ":w": window,
                        ":ttl": ttl,
                        ":one": 1,
                        ":stale": stale,
                        ":max": MAX_TURNS_PER_CHAT,
                    }
                ),
            }
        }
    base = {"pk": pk, "chat_id": chat_id, "window": window, "user_focus": user, "created_at": now, "ttl": ttl}
    try:
        _client().transact_write_items(
            TransactItems=[
                header_op,
                {
                    "Put": {
                        "TableName": table,
                        "Item": _ser(
                            {
                                **base,
                                "sk": user_sk,
                                "msg_id": user_id,
                                "role": "user",
                                "text": message,
                                "status": "complete",
                            }
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": table,
                        "Item": _ser(
                            {
                                **base,
                                "sk": reply_sk,
                                "msg_id": reply_id,
                                "role": "assistant",
                                "text": "",
                                "status": "pending",
                                "request_text": message,
                            }
                        ),
                    }
                },
            ]
        )
    except ClientError as exc:
        if _code(exc) != "TransactionCanceledException":
            raise
        if new_chat:
            raise ChatBusy(chat_id) from exc
        header = _header(owner, chat_id)
        if header is None:
            raise ChatNotFound(chat_id) from exc
        if int(header.get("turn_count", 0) or 0) >= MAX_TURNS_PER_CHAT:
            raise ChatLimit(f"This chat reached {MAX_TURNS_PER_CHAT} questions; start a new chat.") from exc
        raise ChatBusy(chat_id) from exc
    return {"chat_id": chat_id, "message_id": reply_id, "reply_sk": reply_sk}


def claim_reply(owner: str, reply_sk: str) -> dict[str, Any] | None:
    """Claim a pending reply for the worker, once (async deliveries can repeat).

    Returns None for an already claimed or finished reply. Raises ReplyExpired for an
    unclaimed reply created more than CLAIM_MAX_AGE_SECONDS ago (left unclaimed)."""
    now_dt = _now_dt()
    cutoff = _iso(now_dt - timedelta(seconds=CLAIM_MAX_AGE_SECONDS))
    key = {"pk": _pk(owner), "sk": reply_sk}
    try:
        resp = _table().update_item(
            Key=key,
            UpdateExpression="SET #ca = :now",
            ConditionExpression="#s = :pending AND attribute_not_exists(#ca) AND #cr > :cutoff",
            ExpressionAttributeNames={"#s": "status", "#ca": "claimed_at", "#cr": "created_at"},
            ExpressionAttributeValues={":now": _iso(now_dt), ":pending": "pending", ":cutoff": cutoff},
            ReturnValues="ALL_NEW",
        )
        return _from_dynamo(resp.get("Attributes"))
    except ClientError as exc:
        if _code(exc) != "ConditionalCheckFailedException":
            raise
    item = _table().get_item(Key=key, ConsistentRead=True).get("Item") or {}
    if item.get("status") == "pending" and "claimed_at" not in item and str(item.get("created_at")) <= cutoff:
        raise ReplyExpired(reply_sk)
    return None


def finish_reply(owner: str, chat_id: str, reply_sk: str, status: str, result: dict[str, Any]) -> bool:
    """Store the result (status complete|failed) and clear the chat's pending marker.

    Conditional on the reply still existing and pending, so a chat the admin
    deleted mid-answer is never recreated. Returns False when dropped."""
    now = _iso(_now_dt())
    ttl = _ttl()
    try:
        _table().update_item(
            Key={"pk": _pk(owner), "sk": reply_sk},
            UpdateExpression=(
                "SET #s = :s, #t = :t, #f = :f, #c = :c, #d = :d, #n = :n, #ct = :now, #ttl = :ttl REMOVE #rq"
            ),
            ConditionExpression="attribute_exists(#sk) AND #s = :pending",
            ExpressionAttributeNames={
                "#s": "status",
                "#t": "text",
                "#f": "findings",
                "#c": "citations",
                "#d": "citation_details",
                "#n": "notes",
                "#ct": "completed_at",
                "#ttl": "ttl",
                "#rq": "request_text",
                "#sk": "sk",
            },
            ExpressionAttributeValues=_to_dynamo(
                {
                    ":s": status,
                    ":t": result.get("reply", ""),
                    ":f": result.get("findings", []),
                    ":c": result.get("citations", []),
                    ":d": result.get("citation_details", []),
                    ":n": result.get("notes", []),
                    ":now": now,
                    ":ttl": ttl,
                    ":pending": "pending",
                }
            ),
        )
    except ClientError as exc:
        if _code(exc) == "ConditionalCheckFailedException":
            return False
        raise
    try:
        _table().update_item(
            Key={"pk": _pk(owner), "sk": f"CHAT#{chat_id}"},
            UpdateExpression="SET #ua = :now, #ttl = :ttl REMOVE #pm, #ps",
            ConditionExpression="#pm = :r",
            ExpressionAttributeNames={
                "#ua": "updated_at",
                "#ttl": "ttl",
                "#pm": "pending_msg",
                "#ps": "pending_since",
            },
            ExpressionAttributeValues={":now": now, ":ttl": ttl, ":r": reply_sk},
        )
    except ClientError as exc:
        if _code(exc) != "ConditionalCheckFailedException":
            raise
    return True


def set_title(owner: str, chat_id: str, title: str) -> None:
    """Replace the chat's title (the agent's short summary of the first question)."""
    if not title:
        return
    try:
        _table().update_item(
            Key={"pk": _pk(owner), "sk": f"CHAT#{chat_id}"},
            UpdateExpression="SET #ti = :t",
            ConditionExpression="attribute_exists(#sk)",
            ExpressionAttributeNames={"#ti": "title", "#sk": "sk"},
            ExpressionAttributeValues={":t": title[:TITLE_MAX]},
        )
    except ClientError as exc:
        if _code(exc) != "ConditionalCheckFailedException":
            raise


def _delete_keys(keys: list[dict[str, str]]) -> None:
    with _table().batch_writer() as batch:
        for k in keys:
            batch.delete_item(Key=k)


def delete_chat(owner: str, chat_id: str) -> None:
    if _header(owner, chat_id) is None:
        raise ChatNotFound(chat_id)
    items = messages(owner, chat_id) + [{"pk": _pk(owner), "sk": f"CHAT#{chat_id}"}]
    _delete_keys([{"pk": i["pk"], "sk": i["sk"]} for i in items])


def delete_all(owner: str) -> int:
    items = _query(owner)
    _delete_keys([{"pk": i["pk"], "sk": i["sk"]} for i in items])
    return sum(1 for i in items if i["sk"].startswith("CHAT#"))
