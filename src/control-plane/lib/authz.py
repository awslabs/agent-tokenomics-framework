"""Role-based authorization for the control-plane API (threat model M14, T8).

Roles are Cognito groups in the admin pool, read from the verified id-token
claims the API Gateway authorizer injects:

  - governance-admin : read everything; change flags, usage plans and per-user
                       overrides; start benchmarks (they spend Bedrock tokens and
                       pin no-caps governance).
  - viewer           : read-only (analytics, config, benchmarks, audit, Advisor).

A signed-in admin-pool user in neither group gets nothing. Violations raise
PermissionError, which the router maps to 403.
"""

from __future__ import annotations

import re
from typing import Any

ADMIN_GROUP = "governance-admin"
VIEWER_GROUP = "viewer"


def groups(claims: dict[str, Any]) -> set[str]:
    """The caller's groups from the `cognito:groups` claim.

    The REST API Cognito authorizer flattens the JSON array to a string, e.g.
    "governance-admin" or "[governance-admin, viewer]"; accept a list too.
    """
    raw = claims.get("cognito:groups")
    if isinstance(raw, list):
        return {str(g) for g in raw if g}
    if not raw:
        return set()
    return {g for g in re.split(r"[\s,]+", str(raw).strip("[]")) if g}


def can_write(claims: dict[str, Any]) -> bool:
    return ADMIN_GROUP in groups(claims)


def require_read(claims: dict[str, Any]) -> None:
    if not groups(claims) & {ADMIN_GROUP, VIEWER_GROUP}:
        raise PermissionError("no control-plane role")


def require_write(claims: dict[str, Any]) -> None:
    if not can_write(claims):
        raise PermissionError(f"requires the {ADMIN_GROUP} role")
