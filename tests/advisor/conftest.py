"""Fixtures for the live Advisor tests in tests/advisor/, opt-in via the `live` marker.

The unit tests beside them (test_advisor_*.py) are offline, use tests/conftest.py
and run by default: `src/app-plane/.venv/bin/python -m pytest -q tests/advisor`.

Runs against a DEPLOYED stack: endpoints come from CloudFormation outputs, and
credentials come ONLY from the environment (nothing is hard-coded):

    TOKENOMICS_REGION            (default us-west-2)
    TOKENOMICS_ADMIN_EMAIL       admin user of the control-plane pool
    TOKENOMICS_ADMIN_PASSWORD    its password

    src/app-plane/.venv/bin/python -m pytest tests/advisor -m live -v
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any

import boto3
import pytest
import requests

REGION = os.environ.get("TOKENOMICS_REGION", "us-west-2")
CONTROL_STACK = "Tokenomics-ControlPlane"
APP_STACK = "Tokenomics-AppPlane"
POLL_SECONDS = 1.5
GIVE_UP_SECONDS = 200


@lru_cache(maxsize=2)
def outputs(stack: str = CONTROL_STACK) -> dict[str, str]:
    try:
        st = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:
        pytest.skip(f"{stack} not deployed ({type(exc).__name__})")
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def login(pool_id: str, client_id: str, email: str, pw: str) -> str:
    # ADMIN_USER_PASSWORD_AUTH (IAM-gated): the SPA client doesn't allow USER_PASSWORD_AUTH.
    resp = boto3.client("cognito-idp", region_name=REGION).admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": pw},
    )
    return resp["AuthenticationResult"]["IdToken"]


@pytest.fixture(scope="session")
def stack_outputs() -> dict[str, str]:
    return outputs()


@pytest.fixture(scope="session")
def admin_token() -> str:
    email, pw = os.environ.get("TOKENOMICS_ADMIN_EMAIL"), os.environ.get("TOKENOMICS_ADMIN_PASSWORD")
    if not email or not pw:
        pytest.skip("set TOKENOMICS_ADMIN_EMAIL / TOKENOMICS_ADMIN_PASSWORD to run the live Advisor suite")
    return login(outputs()["AdminUserPoolId"], outputs()["AdminUserPoolClientId"], email, pw)


class Advisor:
    def __init__(self, token: str) -> None:
        self.base = outputs()["ApiUrl"].rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, **kw: Any) -> requests.Response:
        return requests.request(method, f"{self.base}{path}", headers=self.h, timeout=30, **kw)

    def ask(self, message: str, window: str = "7d", chat_id: str | None = None) -> tuple[str, dict[str, Any]]:
        body = {"message": message, "window": window, **({"chat_id": chat_id} if chat_id else {})}
        r = self.call("POST", "/advisor/chats", json=body)
        assert r.status_code == 202, r.text
        sent = r.json()
        started = time.monotonic()
        while time.monotonic() - started < GIVE_UP_SECONDS:
            chat = self.call("GET", f"/advisor/chats/{sent['chat_id']}").json()
            reply = next(m for m in chat["messages"] if m["msg_id"] == sent["message_id"])
            if reply["status"] != "pending":
                return sent["chat_id"], reply
            time.sleep(POLL_SECONDS)
        raise AssertionError("advisor did not answer in time")

    def analytics(self, window: str = "7d") -> dict[str, Any]:
        return self.call("GET", f"/governance/analytics?user=all&window={window}").json()

    def delete(self, chat_id: str) -> None:
        self.call("DELETE", f"/advisor/chats/{chat_id}")


@pytest.fixture(scope="session")
def advisor(admin_token: str) -> Advisor:
    return Advisor(admin_token)
