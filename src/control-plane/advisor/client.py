"""Client for the Advisor agent on AgentCore Runtime (used by the AdvisorWorker).

Same pattern as lib/agent_client.py, with the Advisor's timeout ladder: one
attempt only (a retry would re-run -- and re-bill -- the whole agent turn), a
read timeout just above the agent's own deadline, and StopRuntimeSession on
timeout so an abandoned turn stops spending.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ReadTimeoutError

import config

READ_TIMEOUT_SECONDS = 170
# AgentCore returns 409 RetryableConflictException while a session is provisioning
# or terminating; it is raised before any work, so one delayed retry is safe.
_CONFLICT_RETRY_DELAY_SECONDS = 1.0


logger = logging.getLogger(__name__)


class AdvisorTimeout(RuntimeError):
    """The agent did not answer within the read timeout (session was stopped)."""


class AdvisorRuntimeError(RuntimeError):
    """The runtime call failed or returned an unusable response."""


@lru_cache(maxsize=4)
def _client(read_timeout: int = READ_TIMEOUT_SECONDS):
    """A shorter read_timeout bounds a retry by the Lambda's remaining time."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=config.AWS_REGION,
        config=BotoConfig(connect_timeout=2, read_timeout=read_timeout, retries={"total_max_attempts": 1}),
    )


@lru_cache(maxsize=1)
def _stop_client():
    return boto3.client(
        "bedrock-agentcore",
        region_name=config.AWS_REGION,
        config=BotoConfig(connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1}),
    )


def session_id(owner: str, chat_id: str) -> str:
    """Stable per-chat session (33-256 chars, AgentCore requirement)."""
    return f"advisor-{owner}-{chat_id}"[:256]


def invoke(
    payload: dict[str, Any], runtime_session_id: str, read_timeout: int | None = None
) -> dict[str, Any]:
    """read_timeout (seconds) replaces READ_TIMEOUT_SECONDS for this call when given."""
    if not config.ADVISOR_RUNTIME_ARN:
        raise AdvisorRuntimeError("advisor runtime is not configured")
    body = json.dumps(payload).encode("utf-8")
    client = _client(int(read_timeout)) if read_timeout else _client()
    for attempt in range(2):
        try:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=config.ADVISOR_RUNTIME_ARN,
                runtimeSessionId=runtime_session_id,
                qualifier=config.AGENT_RUNTIME_QUALIFIER,
                payload=body,
            )
            break
        except ReadTimeoutError as exc:
            stop(runtime_session_id)
            raise AdvisorTimeout("advisor timed out") from exc
        except ClientError as exc:
            # 409: the session is provisioning/terminating -- raised before any work, safe to retry once.
            if exc.response.get("Error", {}).get("Code") == "RetryableConflictException" and attempt == 0:
                # Intentional backoff (see _CONFLICT_RETRY_DELAY_SECONDS), not a leftover sleep.
                time.sleep(_CONFLICT_RETRY_DELAY_SECONDS)  # nosemgrep: arbitrary-sleep
                continue
            raise AdvisorRuntimeError("advisor runtime call failed") from exc
    try:
        data = json.loads(resp["response"].read())
    except (ReadTimeoutError, TimeoutError) as exc:
        stop(runtime_session_id)
        raise AdvisorTimeout("advisor timed out") from exc
    except Exception as exc:
        raise AdvisorRuntimeError("could not parse advisor response") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise AdvisorRuntimeError("advisor returned an error")
    return data


def stop(runtime_session_id: str) -> None:
    """Best-effort: terminate the session so an abandoned turn stops spending."""
    try:
        _stop_client().stop_runtime_session(
            agentRuntimeArn=config.ADVISOR_RUNTIME_ARN,
            runtimeSessionId=runtime_session_id,
            qualifier=config.AGENT_RUNTIME_QUALIFIER,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[advisor-client] stop_runtime_session failed (%s)", type(exc).__name__)
