"""Seed-user passwords: never committed, never generated, never printed.

A password comes only from an environment variable the operator supplies. A NEW
user is created only when its variable is set; otherwise the seed skips it and
says which variable to set. An EXISTING user's password is reset only when the
variable is set, so a redeploy never changes a password that is already in use.
Pools require 12+ chars with upper, lower, digit and symbol (infra/stacks/*_stack.py).
"""

from __future__ import annotations

import os


def from_env(var: str) -> str | None:
    """The operator-supplied password in `var`, or None if unset/blank."""
    return (os.environ.get(var) or "").strip() or None


def user_exists(cog, pool_id: str, email: str) -> bool:
    try:
        cog.admin_get_user(UserPoolId=pool_id, Username=email)
        return True
    except cog.exceptions.UserNotFoundException:
        return False


def report_missing(email: str, env_var: str) -> None:
    """Tell the operator a new user was skipped because no password was supplied."""
    print(f"  WARNING: {email} not created -- no password supplied.")
    print(f"    Set {env_var} to a strong password and re-run the deploy.")
