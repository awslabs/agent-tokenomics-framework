"""Shared fixtures for the live benchmarking test suite (tests/benchmarking/).

These tests run against a DEPLOYED stack (not mocks). They:
  * discover the admin API + pool/client ids and the Metrics table name from the
    control-plane CloudFormation stack outputs (no hardcoded ids — works in any
    account/region the stack is deployed to),
  * mint a real Cognito token for the shipped admin user,
  * start a REAL benchmark through the admin API (the agent actually answers each
    turn on AgentCore) and poll it to completion.

A benchmark is isolated by design: it pins a no-caps governance config + its
feature flags PER REQUEST (never writes the live Flags / UsagePlans tables), and
its turns are tagged `source=benchmark` into separate `BENCH#RUN#` metric rows.
So these tests mutate NO shared config and never pollute live analytics — there
is nothing to snapshot/restore.

Run them explicitly (opt-in via the `live` marker):

    bash tests/benchmarking/test-benchmarking.sh
    src/app-plane/.venv/bin/python -m pytest tests/benchmarking -m live -v -s

Prerequisites:
  * the stack is deployed (control plane + app plane; the agent must be reachable),
  * AWS credentials for that account are in the environment (same creds the
    `aws` CLI uses),
  * the app-plane venv (it has boto3 + requests),
  * Bedrock model access enabled in the region for the agent's models.

Env overrides (optional):
  * TOKENOMICS_REGION          (default: us-west-2)
  * TOKENOMICS_ADMIN_PASSWORD  (required)  — control-plane admin (the same
    value set at deploy time; missing -> skip)
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3
import pytest
import requests

REGION = os.environ.get("TOKENOMICS_REGION", "us-west-2")
ADMIN_PASSWORD = os.environ.get("TOKENOMICS_ADMIN_PASSWORD", "")

ADMIN_EMAIL = "admin@example.com"
CONTROL_STACK = "Tokenomics-ControlPlane"

HTTP_TIMEOUT = 60


@lru_cache(maxsize=4)
def _stack_outputs(stack_name: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except Exception as exc:
        pytest.skip(f"stack {stack_name} not found ({exc}); deploy before running live tests")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


class StackInfo:
    """Resolved live admin endpoint + identity config for the deployed stack."""

    def __init__(self) -> None:
        cp = _stack_outputs(CONTROL_STACK)
        self.admin_api = cp["ApiUrl"].rstrip("/")
        self.admin_pool_id = cp["AdminUserPoolId"]
        self.admin_client_id = cp["AdminUserPoolClientId"]
        self.metrics_table = cp["MetricsTableName"]


@pytest.fixture(scope="session")
def stack() -> StackInfo:
    return StackInfo()


def _id_token(pool_id: str, client_id: str, username: str, password: str) -> str:
    # ADMIN_USER_PASSWORD_AUTH (IAM-gated): the SPA client doesn't allow USER_PASSWORD_AUTH.
    if not password:
        pytest.skip(f"no password for {username}; set TOKENOMICS_ADMIN_PASSWORD")
    idp = boto3.client("cognito-idp", region_name=REGION)
    try:
        resp = idp.admin_initiate_auth(
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as exc:
        pytest.skip(f"cannot authenticate {username} ({exc}); check the seeded admin user / password")
    tok = resp.get("AuthenticationResult", {}).get("IdToken")
    if not tok:
        pytest.skip(f"no IdToken for {username} (challenge?: {resp.get('ChallengeName')})")
    return tok


@pytest.fixture(scope="session")
def admin_token(stack: StackInfo) -> str:
    return _id_token(stack.admin_pool_id, stack.admin_client_id, ADMIN_EMAIL, ADMIN_PASSWORD)


class BenchClient:
    """Minimal client for the control-plane /benchmarks API, over HTTPS."""

    def __init__(self, stack: StackInfo, admin_token: str) -> None:
        self._s = stack
        self._admin = admin_token

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(
            f"{self._s.admin_api}/benchmarks",
            headers={"Authorization": f"Bearer {self._admin}", "Content-Type": "application/json"},
            json=body,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()

    def get(self, benchmark_id: str) -> dict[str, Any]:
        r = requests.get(
            f"{self._s.admin_api}/benchmarks/{benchmark_id}",
            headers={"Authorization": f"Bearer {self._admin}"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="session")
def bench(stack: StackInfo, admin_token: str) -> BenchClient:
    return BenchClient(stack, admin_token)
