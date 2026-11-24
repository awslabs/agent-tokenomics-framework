"""Unit tests for seed-user passwords (threat model T4) -- no AWS.

The seed must never ship, generate or print a password, and must never silently
reset one an operator already uses:
  * a NEW user with no supplied password is skipped (not created) with a warning
    naming the env var to set;
  * an EXISTING user's password is left alone unless one is supplied;
  * a supplied password (env var) is applied to new and existing users alike and
    never printed;
  * no password is committed in seed/data/users.json.

Run:
    src/app-plane/.venv/bin/python -m pytest tests/test_seed_credentials.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for p in (_ROOT / "src", _ROOT / "seed"):
    sys.path.insert(0, str(p))

import credentials  # noqa: E402
import load_app_plane  # noqa: E402
import load_control_plane  # noqa: E402


class _FakeCognito:
    """Records password sets; `existing` users raise UsernameExistsException on create."""

    class exceptions:
        class UsernameExistsException(Exception):
            pass

        class UserNotFoundException(Exception):
            pass

    def __init__(self, existing: set[str] = frozenset()) -> None:
        self.existing = set(existing)
        self.passwords: dict[str, str] = {}
        self.groups: dict[str, set[str]] = {}

    def admin_create_user(self, *, Username, **_):
        if Username in self.existing:
            raise self.exceptions.UsernameExistsException()
        self.existing.add(Username)
        return {"User": {"Attributes": []}}

    def admin_update_user_attributes(self, **_):
        pass

    def admin_get_user(self, *, Username, **_):
        if Username not in self.existing:
            raise self.exceptions.UserNotFoundException()
        return {"UserAttributes": [{"Name": "sub", "Value": f"sub-{Username}"}]}

    def admin_set_user_password(self, *, Username, Password, Permanent, **_):
        assert Permanent is True
        self.passwords[Username] = Password

    def admin_add_user_to_group(self, *, Username, GroupName, **_):
        self.groups.setdefault(Username, set()).add(GroupName)


def test_from_env_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("X_PW", "   ")
    assert credentials.from_env("X_PW") is None
    monkeypatch.setenv("X_PW", "S3cure!Passw0rd")
    assert credentials.from_env("X_PW") == "S3cure!Passw0rd"
    monkeypatch.delenv("X_PW")
    assert credentials.from_env("X_PW") is None


@pytest.mark.parametrize(
    "seed",
    [
        lambda cog, pw: load_app_plane._upsert_cognito_user(cog, "pool", {"email": "u@example.com"}, pw),
        lambda cog, pw: load_control_plane._seed_admin(cog, "pool", "u@example.com", pw),
    ],
    ids=["app-user", "admin"],
)
class TestSeedPassword:
    def test_new_user_without_supplied_password_is_skipped(self, seed, capsys):
        cog = _FakeCognito()
        seed(cog, None)
        assert cog.existing == set() and cog.passwords == {} and cog.groups == {}
        out = capsys.readouterr().out
        assert "not created" in out and "TOKENOMICS_" in out

    def test_existing_user_password_untouched_without_supplied_password(self, seed, capsys):
        cog = _FakeCognito(existing={"u@example.com"})
        seed(cog, None)
        assert cog.passwords == {}
        assert "not created" not in capsys.readouterr().out

    @pytest.mark.parametrize("existing", [set(), {"u@example.com"}])
    def test_supplied_password_is_applied_and_not_printed(self, seed, existing, capsys):
        cog = _FakeCognito(existing=existing)
        seed(cog, "Operator!Chosen1")
        assert cog.passwords == {"u@example.com": "Operator!Chosen1"}
        assert "Operator!Chosen1" not in capsys.readouterr().out


def test_admin_is_governance_admin_and_viewer_is_read_only():
    cog = _FakeCognito()
    load_control_plane._seed_admin(cog, "pool", "admin@example.com", "Operator!Chosen1")
    load_control_plane._seed_admin(
        cog, "pool", "viewer@example.com", "Operator!Chosen2", group=load_control_plane.VIEWER_GROUP
    )
    assert cog.groups == {"admin@example.com": {"governance-admin"}, "viewer@example.com": {"viewer"}}


def test_no_password_committed_in_seed_data():
    users = json.loads((_ROOT / "seed" / "data" / "users.json").read_text(encoding="utf-8"))
    assert users and all("password" not in u for u in users)
