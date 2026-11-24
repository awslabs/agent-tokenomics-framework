"""Application-plane configuration (cloud-only).

Everything is env-driven so the container runs unmodified on AgentCore Runtime.
The DynamoDB table names and the flags table are injected by the deploy tooling
(CDK exports -> AgentCore Runtime environment variables).
"""

from __future__ import annotations

import os

from shared import price_map

# ---- AWS / Bedrock ---------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

# Per-agent model (inference-profile ids). Each agent picks its own model from
# config, so they can differ (e.g. a frontier orchestrator + cheaper specialists)
# without touching code. The default tiers come from the model-id single source
# (shared/price_map), so a model swap happens in exactly one place.
ORCHESTRATOR_MODEL = os.environ.get("ORCHESTRATOR_MODEL", price_map.SONNET_5_5)
KNOWLEDGE_MODEL = os.environ.get("KNOWLEDGE_MODEL", price_map.HAIKU_5_5)
ORDER_MODEL = os.environ.get("ORDER_MODEL", price_map.HAIKU_5_5)

# Model-routing pattern: the LLM-as-judge classifier for the orchestrator's
# route-down decision. Judge ONLY -- never an agent's runtime model (it is a
# different, smaller context window than the Sonnet/Haiku agent models, so it is
# never a routing candidate). Cheapest model in price_map, so the per-step judge
# call stays well below the Sonnet->Haiku saving it unlocks (WA AGENTCOST02-BP01).
ROUTER_JUDGE_MODEL = os.environ.get("ROUTER_JUDGE_MODEL", price_map.NOVA_LITE)


# Bedrock Guardrail on the orchestrator (threat model M11, T5): blocks prompt
# attacks / jailbreaks in the customer's message and harmful content either way.
# Empty (local dev / unit tests) => no guardrail.
SUPPORT_GUARDRAIL_ID = os.environ.get("SUPPORT_GUARDRAIL_ID", "")
SUPPORT_GUARDRAIL_VERSION = os.environ.get("SUPPORT_GUARDRAIL_VERSION", "")


# ---- DynamoDB tables (mock business backend + shared flags) ----------------
# Tables are auto-named by CDK; their real names are injected as env vars at
# deploy time. No hardcoded name defaults (they would be wrong once auto-named).
# User profile lives in Cognito (no Customers table). Orders keyed by Cognito sub.

ORDERS_TABLE = os.environ["ORDERS_TABLE"]
FAQ_TABLE = os.environ["FAQ_TABLE"]
FLAGS_TABLE = os.environ["FLAGS_TABLE"]

# ---- Token governance ------------------------------------------------------
# Shared usage-plans/quotas config table (control writes, agent reads). Optional
# (like memory): empty => the agent runs with NO governance caps, so a missing
# table degrades to baseline behaviour rather than crashing.
USAGE_PLANS_TABLE = os.environ.get("USAGE_PLANS_TABLE", "")

# EventBridge bus the agent emits per-turn usage events to (metering). Optional:
# empty => no emission (the app plane never depends on the control plane).
METERING_BUS = os.environ.get("METERING_BUS", "")

# Enforcement counter table (DynamoDB). The agent reads/writes:
#   SESSION#<id>            -> soft per-session cap (session_counter.py)
#   USER#<sub>#DAY#<date>   -> per-user daily quota  (quota_hook.py)
#   USER#<sub>#MONTH#<ym>   -> per-user monthly quota (quota_hook.py)
#   USER#<sub>#MIN#<ts>     -> per-user TPM throttle window (quota_hook.py)
# Date-keyed rows + TTL = implicit reset (no rollover Lambda). Optional: empty =>
# session cap + per-user quota/throttle are skipped (baseline). TTL self-expires
# session + daily/minute rows; monthly rows get a longer floor (see quota_hook).
ENFORCEMENT_COUNTER_TABLE = os.environ.get("ENFORCEMENT_COUNTER_TABLE", "")
ENFORCEMENT_COUNTER_TTL_DAYS = int(os.environ.get("ENFORCEMENT_COUNTER_TTL_DAYS", "3"))

# ---- AgentCore Memory (short-term) -----------------------------------------
# Resource id injected by CDK. Optional (unlike tables): if empty, the agent
# runs stateless regardless of the `memory` flag -- graceful degradation so the
# container still boots when memory isn't provisioned.
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

# How long the agent caches a pulled config table in-process before re-reading
# it from DynamoDB. Applies to BOTH shared config readers -- feature flags
# (flags.py) and governance usage plans (plans.py) -- which use the identical
# pull-on-TTL pattern, so they deliberately share one TTL. Keeps config reads
# near-zero cost on the hot path while still applying a control-plane change
# within a couple of seconds.
CONFIG_CACHE_TTL_SECONDS = float(os.environ.get("CONFIG_CACHE_TTL_SECONDS", "2"))
# Back-compat alias (flags.py predates the rename; both names are the same TTL).
FLAGS_CACHE_TTL_SECONDS = CONFIG_CACHE_TTL_SECONDS

# Largest customer message the runtime accepts (threat model T6): stops one
# oversized prompt from burning a turn's token budget. The chat proxy applies the
# same default limit before invoking the runtime.
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "4000"))
