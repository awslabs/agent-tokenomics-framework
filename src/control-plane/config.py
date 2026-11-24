"""Control-plane configuration (cloud-only).

Env-driven so the same handlers run in Lambda. Table names + the AgentCore
runtime ARN are injected by the CDK ControlPlaneStack.
"""

from __future__ import annotations

import os
from pathlib import Path

# Application-plane endpoint: an AgentCore Runtime ARN. The agent_client uses the
# bedrock-agentcore InvokeAgentRuntime API to reach it.
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENT_RUNTIME_QUALIFIER = os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT")
# KMS HMAC key the benchmark worker signs runtime requests with (threat model M8).
BENCHMARK_MAC_KEY_ARN = os.environ.get("BENCHMARK_MAC_KEY_ARN", "")

# Async benchmark worker Lambda name (injected by ControlPlaneStack). The API
# Lambda async-invokes this function to run the N x M loop off the frozen request env.
BENCHMARK_WORKER_FUNCTION_NAME = os.environ.get("BENCHMARK_WORKER_FUNCTION_NAME", "")
AGENT_TIMEOUT_SECONDS = float(os.environ.get("AGENT_TIMEOUT_SECONDS", "120"))

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

# DynamoDB tables: auto-named by CDK, injected as env vars by ControlPlaneStack.
FLAGS_TABLE = os.environ["FLAGS_TABLE"]
RUNS_TABLE = os.environ["RUNS_TABLE"]

# Token-governance tables (injected by ControlPlaneStack).
#   USAGE_PLANS_TABLE  -> shared config (control writes, agent reads)
#   METRICS_TABLE      -> control-plane metrics/ledger (EventBridge consumer writes)
USAGE_PLANS_TABLE = os.environ.get("USAGE_PLANS_TABLE", "")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "")

# Append-only audit trail of governance changes (threat model M16; lib/audit.py).
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")

# Retention for the Advisor's per-session turn ledger rows (TURNS#<session_id>)
# in the Metrics table; the row's `ttl` is refreshed on every appended turn.
TURN_TTL_DAYS = int(os.environ.get("TURN_TTL_DAYS", "90"))

# App-plane Cognito pool id, so the admin API can resolve a customer email -> sub
# when creating a per-user quota override (records are keyed by sub, shown by email).
APP_USER_POOL_ID = os.environ.get("APP_USER_POOL_ID", "")

# Benchmark scenario set (fixed multi-turn corpus). Packaged with the Lambda.
SCENARIOS_FILE = Path(
    os.environ.get(
        "TOKENOMICS_SCENARIOS_FILE",
        Path(__file__).resolve().parent / "benchmarking" / "scenarios.json",
    )
)

# ---- Tokenomics Advisor ------------------------------------------------------
# Pattern catalog bundled with the Advisor runtime image and the AdvisorWorker
# (copied from seed/data/advisor/tokenomics-patterns-kb.md at build/synth time).
ADVISOR_CATALOG_FILE = Path(
    os.environ.get(
        "ADVISOR_CATALOG_FILE",
        Path(__file__).resolve().parent / "advisor" / "tokenomics-patterns-kb.md",
    )
)
# Bedrock Knowledge Base (S3 Vectors) holding the catalog; empty => keyword fallback.
ADVISOR_KB_ID = os.environ.get("ADVISOR_KB_ID", "")
ADVISOR_KB_MIN_SCORE = float(os.environ.get("ADVISOR_KB_MIN_SCORE", "0.2"))
# AdvisorChats table (per-admin conversations + reply jobs) and the async worker.
ADVISOR_CHATS_TABLE = os.environ.get("ADVISOR_CHATS_TABLE", "")
ADVISOR_CHAT_TTL_DAYS = int(os.environ.get("ADVISOR_CHAT_TTL_DAYS", "90"))
ADVISOR_WORKER_FUNCTION_NAME = os.environ.get("ADVISOR_WORKER_FUNCTION_NAME", "")
# Advisor AgentCore runtime (invoked by the worker).
ADVISOR_RUNTIME_ARN = os.environ.get("ADVISOR_RUNTIME_ARN", "")

# Benchmark concurrency (parallel conversations per run worker).
BENCHMARK_CONCURRENCY = int(os.environ.get("BENCHMARK_CONCURRENCY", "4"))
