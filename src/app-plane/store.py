"""DynamoDB-backed data store for the mock business backend.

Orders are keyed by the user's Cognito `sub` (`user_id`); the FAQ is a small
table scanned once and keyword-matched in Python (stands in for a Bedrock KB).
User profile is NOT stored here -- it lives in Cognito and is forwarded to the
agent via the request (see tokenomics/turn_context.py).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

import config
from shared import dynamo

# DynamoDB Decimal -> plain int/float so tool results are JSON-safe. The
# conversion lives in shared/dynamo (one copy for both planes); alias it locally.
_plain = dynamo.to_plain


@lru_cache(maxsize=1)
def _dynamodb():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION)


@lru_cache(maxsize=1)
def _orders_table():
    return _dynamodb().Table(config.ORDERS_TABLE)


@lru_cache(maxsize=1)
def _faq_table():
    return _dynamodb().Table(config.FAQ_TABLE)


@lru_cache(maxsize=1)
def _faq_items() -> tuple[dict[str, Any], ...]:
    """FAQ is static + tiny; scan once and cache in-process for the container."""
    resp = _faq_table().scan()
    return tuple(resp.get("Items", []))


def get_orders_for_user(user_id: str) -> list[dict[str, Any]]:
    """All orders for a user, keyed by their Cognito sub."""
    uid = (user_id or "").strip()
    if not uid:
        return []
    resp = _orders_table().query(KeyConditionExpression=Key("user_id").eq(uid))
    return [_plain(o) for o in resp.get("Items", [])]


def search_faq(query: str, limit: int = 2) -> list[dict[str, Any]]:
    """Keyword match over the FAQ table (stands in for a Bedrock KB).

    Returns the best-matching entries by keyword overlap. Deterministic so
    benchmark runs are repeatable.
    """
    q = (query or "").lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in _faq_items():
        score = sum(1 for kw in entry.get("keywords", []) if kw in q)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda t: (-t[0], t[1]["id"]))
    return [_plain(e) for _, e in scored[:limit]]
