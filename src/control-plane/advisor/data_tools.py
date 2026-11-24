"""Advisor data tools -- the Advisor agent's read-only tools, run in-process.

Called by the agent's @tool wrappers (advisor/agent/tools.py) as
    dispatch("<name>", {...})
and returns the tool's JSON result. Only allow-listed tools run; inputs are
validated again here (the wrappers validate too). Errors come back as
{"error": "<safe message>"} -- never exception text.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

import boto3
import botocore.session
from botocore.config import Config as BotoConfig

from advisor import catalog as advisor_catalog
from advisor import kb as advisor_kb
from advisor import service as svc
from handlers import governance
from lib import store

# tool name -> allowed argument names
_ARGS = {
    "get_cost_summary": ("window", "user"),
    "get_period_comparison": ("window", "user"),
    "get_cache_health": ("window", "agent_name"),
    "get_outliers": ("window",),
    "get_user_profile": ("window", "user"),
    "get_plan_vs_actual": ("window", "user"),
    "get_governance_config": (),
    "get_turn_curve": ("session_id",),
    "search_patterns": ("query", "k"),
    "get_catalog_index": (),
}


logger = logging.getLogger(__name__)


def dispatch(tool: str, args: dict[str, Any], turn: svc.TurnContext | None = None) -> dict[str, Any]:
    """`turn` is the calling agent turn's context (one load per turn); without one, a fresh load."""
    if tool not in _ARGS:
        return {"error": "unknown tool"}
    if not isinstance(args, dict):
        return {"error": "args must be an object"}
    kwargs = {k: v for k, v in args.items() if k in _ARGS[tool] and v is not None}
    try:
        if tool == "search_patterns":
            return advisor_kb.search_patterns(**kwargs)
        if tool == "get_catalog_index":
            return {"tool": tool, "index": advisor_catalog.index_text(advisor_catalog.load())}
        result = svc.TOOLS[tool]((turn or svc.TurnContext()).get(), **kwargs)
        for s in result.get("signals") or []:
            s["patterns"] = advisor_kb.pattern_details(s.get("patterns") or [])
        return result
    except svc.AdvisorInputError as exc:
        out: dict[str, Any] = {"error": str(exc)}
        if exc.candidates:
            out["candidates"] = exc.candidates
        return out
    except (ValueError, TypeError):
        logger.warning("[advisor-tools] %s rejected its arguments\n%s", tool, traceback.format_exc())
        return {"error": "invalid arguments for this tool"}
    except Exception:  # noqa: BLE001 -- never leak internals to the model; the log keeps the trace
        logger.warning("[advisor-tools] %s failed\n%s", tool, traceback.format_exc())
        return {"error": "tool failed; try again or ask a different question"}


# Default for every boto3 client this process creates (the store's DynamoDB and the Cognito
# clients included; botocore merges it into each client's own config), so a hung AWS call ends
# within seconds and frees its tool slot instead of waiting out the SDK's 60 s / multi-retry defaults.
_CLIENT_DEFAULTS = BotoConfig(
    connect_timeout=2, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 2}
)


def warm() -> None:
    """Bound the AWS clients, create them and load the catalog once, before serving. Client
    creation is not thread-safe, and the agent runs a turn's tool calls in parallel threads."""
    session = botocore.session.get_session()
    session.set_default_client_config(_CLIENT_DEFAULTS)
    boto3.setup_default_session(botocore_session=session)
    store._metrics_table()
    store._usage_plans_table()
    store._flags_table()
    governance._cognito()
    advisor_kb._client()
    advisor_catalog.load()
