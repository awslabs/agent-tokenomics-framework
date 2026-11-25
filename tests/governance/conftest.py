"""Shared fixtures for the live governance test suite (tests/governance/).

These tests run against a DEPLOYED stack (not mocks). They:
  * discover every endpoint / pool / client id from CloudFormation stack outputs
    (no hardcoded ids — works in any account/region the stack is deployed to),
  * mint real Cognito tokens for the shipped admin + demo users (bob, eva),
  * snapshot the governance config at session start and ALWAYS restore it at
    teardown (even on failure), so a run never leaves the dev's settings changed.

Run them explicitly (they are opt-in via the `live` marker):

    bash tests/governance/test-governance.sh                 # everything
    src/app-plane/.venv/bin/python -m pytest tests/governance -m live -v

Prerequisites:
  * the stack is deployed (control plane + app plane),
  * AWS credentials for that account are in the environment (same creds the
    `aws` CLI uses),
  * the app-plane venv (it has boto3 + requests).

Env:
  * TOKENOMICS_REGION         (default: us-west-2)
  * TOKENOMICS_USER_PASSWORD  (required) — demo chat users (bob/eva)
  * TOKENOMICS_ADMIN_PASSWORD (required) — control-plane admin
  Export the same passwords you entered at the scripts/deploy-all.sh prompts
  (Chat UI / Admin UI). Missing -> tests skip.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3
import pytest
import requests

REGION = os.environ.get("TOKENOMICS_REGION", "us-west-2")
USER_PASSWORD = os.environ.get("TOKENOMICS_USER_PASSWORD", "")
ADMIN_PASSWORD = os.environ.get("TOKENOMICS_ADMIN_PASSWORD", "")

ADMIN_EMAIL = "admin@example.com"
DEMO_USERS = {"bob": "bob@example.com", "eva": "eva@example.com"}

CONTROL_STACK = "Tokenomics-ControlPlane"
APP_STACK = "Tokenomics-AppPlane"

HTTP_TIMEOUT = 150  # a chat turn can take a while (agent + Bedrock)


# ---------------------------------------------------------------------------
# Stack discovery — read endpoints/pools/clients from CloudFormation outputs
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _stack_outputs(stack_name: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except Exception as exc:
        pytest.skip(f"stack {stack_name} not found ({exc}); deploy before running live tests")
    outs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    return outs


class StackInfo:
    """Resolved live endpoints + identity config for the deployed stack."""

    def __init__(self) -> None:
        cp = _stack_outputs(CONTROL_STACK)
        ap = _stack_outputs(APP_STACK)
        self.admin_api = cp["ApiUrl"].rstrip("/")
        self.admin_pool_id = cp["AdminUserPoolId"]
        self.admin_client_id = cp["AdminUserPoolClientId"]
        self.metrics_table = cp["MetricsTableName"]
        self.chat_api = ap["ChatApiUrl"].rstrip("/")
        self.app_pool_id = ap["UserPoolId"]
        self.app_client_id = ap["UserPoolClientId"]


@pytest.fixture(scope="session")
def stack() -> StackInfo:
    return StackInfo()


# ---------------------------------------------------------------------------
# Cognito token minting (ADMIN_USER_PASSWORD_AUTH -- IAM-gated; the SPA clients
# don't allow plaintext USER_PASSWORD_AUTH)
# ---------------------------------------------------------------------------


def _id_token(pool_id: str, client_id: str, username: str, password: str) -> str:
    if not password:
        pytest.skip(f"no password for {username}; set TOKENOMICS_USER_PASSWORD / TOKENOMICS_ADMIN_PASSWORD")
    idp = boto3.client("cognito-idp", region_name=REGION)
    try:
        resp = idp.admin_initiate_auth(
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as exc:
        pytest.skip(f"cannot authenticate {username} ({exc}); check seed users / passwords")
    tok = resp.get("AuthenticationResult", {}).get("IdToken")
    if not tok:
        pytest.skip(f"no IdToken for {username} (challenge?: {resp.get('ChallengeName')})")
    return tok


@pytest.fixture(scope="session")
def admin_token(stack: StackInfo) -> str:
    return _id_token(stack.admin_pool_id, stack.admin_client_id, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def user_tokens(stack: StackInfo) -> dict[str, str]:
    """IdTokens for the shipped demo users, keyed by short name (bob/eva)."""
    return {
        name: _id_token(stack.app_pool_id, stack.app_client_id, email, USER_PASSWORD)
        for name, email in DEMO_USERS.items()
    }


# ---------------------------------------------------------------------------
# Thin API client bound to the live endpoints + admin token
# ---------------------------------------------------------------------------


class GovClient:
    """Minimal client for the governance API + the chat API, over HTTPS."""

    def __init__(self, stack: StackInfo, admin_token: str, user_tokens: dict[str, str]) -> None:
        self._s = stack
        self._admin = admin_token
        self._users = user_tokens

    # ---- control-plane governance API ----
    def get_config(self) -> dict[str, Any]:
        r = requests.get(
            f"{self._s.admin_api}/governance/config",
            headers={"Authorization": f"Bearer {self._admin}"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def put_config(self, partial: dict[str, Any]) -> None:
        r = requests.put(
            f"{self._s.admin_api}/governance/config",
            headers={"Authorization": f"Bearer {self._admin}", "Content-Type": "application/json"},
            json=partial,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()

    def analytics(self, user: str = "all", window: str = "7d") -> dict[str, Any]:
        r = requests.get(
            f"{self._s.admin_api}/governance/analytics",
            params={"user": user, "window": window},
            headers={"Authorization": f"Bearer {self._admin}"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    # ---- app-plane chat API (as a demo user) ----
    def chat(self, user: str, message: str, conversation_id: str) -> requests.Response:
        """Send one chat turn as a demo user. Returns the raw Response so the
        caller can assert on both the HTTP status (403/429) and the body."""
        return requests.post(
            f"{self._s.chat_api}/chat",
            headers={"Authorization": f"Bearer {self._users[user]}", "Content-Type": "application/json"},
            json={"message": message, "conversation_id": conversation_id},
            timeout=HTTP_TIMEOUT,
        )


@pytest.fixture(scope="session")
def gov(stack: StackInfo, admin_token: str, user_tokens: dict[str, str]) -> GovClient:
    return GovClient(stack, admin_token, user_tokens)


# ---------------------------------------------------------------------------
# Config snapshot / restore — the ONLY state these tests mutate
# ---------------------------------------------------------------------------


def _config_to_partial(cfg: dict[str, Any]) -> dict[str, Any]:
    """Shape a full GET /governance/config into the PUT partial the API expects
    (agents + session_max_tokens + user_default sans server-managed fields)."""
    ud = dict(cfg.get("user_default") or {})
    ud.pop("email", None)  # server-managed; not part of the editable quota
    return {
        "agents": cfg.get("agents") or {},
        "session_max_tokens": cfg.get("session_max_tokens", 0),
        "user_default": {
            "daily_max_tokens_per_user": ud.get("daily_max_tokens_per_user", 0),
            "monthly_max_tokens_per_user": ud.get("monthly_max_tokens_per_user", 0),
            "max_tokens_per_minute_per_user": ud.get("max_tokens_per_minute_per_user", 0),
        },
    }


@pytest.fixture
def config_guard(gov: GovClient):
    """Snapshot the governance config, yield a setter, and ALWAYS restore.

    Enforcement tests use this to break a single setting and have the original
    config put back no matter how the test ends.
    """
    original = _config_to_partial(gov.get_config())

    def set_partial(partial: dict[str, Any]) -> None:
        gov.put_config(partial)

    try:
        yield set_partial
    finally:
        gov.put_config(original)
