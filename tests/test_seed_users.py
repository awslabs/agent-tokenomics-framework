"""Unit tests for demo-user provisioning (threat model T4) -- no AWS.

Users are created by the deploy scripts (scripts/common/common.sh
ensure_cognito_user) with passwords prompted by scripts/deploy-all.sh; the seed
only loads data. These tests cover:
  * the password policy check the prompt uses (mirrors the Cognito pool policy);
  * the app-plane seed resolving an existing user's sub, and failing clearly
    for a missing user (it never creates users);
  * no password committed in seed/data/users.json.

Run:
    infra/.venv/bin/python -m pytest tests/test_seed_users.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for p in (_ROOT / "src", _ROOT / "seed"):
    sys.path.insert(0, str(p))

import load_app_plane  # noqa: E402

_COMMON_SH = _ROOT / "scripts" / "common" / "common.sh"


def _password_ok(pw: str) -> bool:
    # Test-only: the password is a bash argv here. In the deploy it comes from
    # a hidden prompt and reaches python/aws on stdin, never on a command line.
    script = f'source "{_COMMON_SH}"; password_ok "$1"'
    return subprocess.run(["bash", "-c", script, "_", pw], check=False).returncode == 0


@pytest.mark.parametrize(
    "pw",
    ["Operator!Chosen1", "Abcdefghij1-", "Xy9 has spaces^", "Q1w2e3r4t5y6'"],
)
def test_password_policy_accepts_valid(pw):
    assert _password_ok(pw)


@pytest.mark.parametrize(
    "pw",
    [
        "Short!1a",  # < 12 chars
        "alllowercase!1",  # no uppercase
        "ALLUPPERCASE!1",  # no lowercase
        "NoDigitsHere!!",  # no digit
        "NoSymbols12345",  # no symbol
        " Leading!Space1",  # leading space
        "Trailing!Space1 ",  # trailing space
        "Euro€Symbol12",  # non-Cognito symbol only
    ],
)
def test_password_policy_rejects_invalid(pw):
    assert not _password_ok(pw)


class _FakeCognito:
    class exceptions:
        class UserNotFoundException(Exception):
            pass

    def __init__(self, existing: set[str] = frozenset()) -> None:
        self.existing = set(existing)

    def admin_get_user(self, *, Username, **_):
        if Username not in self.existing:
            raise self.exceptions.UserNotFoundException()
        return {"UserAttributes": [{"Name": "sub", "Value": f"sub-{Username}"}]}


def test_seed_resolves_existing_user_sub():
    cog = _FakeCognito(existing={"u@example.com"})
    assert load_app_plane._resolve_sub(cog, "pool", "u@example.com") == "sub-u@example.com"


def test_seed_fails_clearly_for_missing_user():
    with pytest.raises(SystemExit, match=r"deploy-all\.sh"):
        load_app_plane._resolve_sub(_FakeCognito(), "pool", "u@example.com")


def test_no_password_committed_in_seed_data():
    users = json.loads((_ROOT / "seed" / "data" / "users.json").read_text(encoding="utf-8"))
    assert users and all("password" not in u for u in users)
