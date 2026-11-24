"""Append-only audit trail of governance changes (threat model M16, T8/T11).

Every change made through the control-plane API (flags, usage plans, per-user
overrides, benchmark starts) writes one record: who (Cognito sub + email), when,
what route, and the config before and after. The API role can only PutItem
(conditional on a new key) and Query this table -- it cannot update or delete
records.

Layout: pk = "AUDIT" (one partition; admin change volume is tiny), sk =
"<ISO ts>#<uuid>" so a descending query returns newest first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

import config
from shared import dynamo

_PK = "AUDIT"


@lru_cache(maxsize=1)
def _table():
    return boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.AUDIT_TABLE)


def record(
    claims: dict[str, Any],
    action: str,
    *,
    before: Any = None,
    after: Any = None,
) -> dict[str, Any]:
    """Persist one audit entry; raises if it can't (the caller surfaces a 500)."""
    ts = datetime.now(UTC).isoformat()
    item = {
        "pk": _PK,
        "sk": f"{ts}#{uuid.uuid4().hex[:12]}",
        "ts": ts,
        "action": action,
        "actor_sub": str(claims.get("sub") or ""),
        "actor_email": str(claims.get("email") or ""),
        "before": before,
        "after": after,
    }
    _table().put_item(Item=dynamo.to_dynamo(item), ConditionExpression="attribute_not_exists(sk)")
    return item


def list_entries(limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first audit entries."""
    resp = _table().query(
        KeyConditionExpression=Key("pk").eq(_PK),
        ScanIndexForward=False,
        Limit=max(1, min(int(limit), 500)),
    )
    return [dynamo.to_plain(i) for i in resp.get("Items", [])]
