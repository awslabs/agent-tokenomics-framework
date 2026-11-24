# Solution Architecture (current, as-built)

The **current, as-built** architecture of the Agent Tokenomics Framework: a
customer-support agent (app plane) whose token cost is made visible and
controllable through a Tokenomics control plane. Everything is deployed on AWS
via CDK/CloudFormation.

This is the **living** architecture doc — kept in sync with what is actually
deployed.

Two independent planes, one shared contract:

- **Application plane** — the Strands support agent on Amazon Bedrock AgentCore
  Runtime, its business data, customer auth + profile (Cognito), short-term
  conversation memory (AgentCore Memory), and the customer chat UI. Owns customer
  traffic.
- **Control plane** — an admin API + UI (behind a dedicated admin Cognito pool)
  for feature flags and before/after benchmarks. Owns admin traffic.
- **Shared** — a single DynamoDB `Flags` table: the control plane writes it, the
  agent reads it. This is the only data coupling between the planes.

---

## High-level architecture

```
                        ┌──────────────────────────┐   ┌──────────────────────────┐
                        │   Customer (shopper)      │   │   Admin / operator        │
                        │   browser                 │   │   browser                 │
                        └────────────┬──────────────┘   └────────────┬─────────────┘
                                     │ HTTPS                          │ HTTPS
              ┌──────────────────────▼───────────┐   ┌────────────────▼──────────────────┐
              │  Chat UI  (S3 + CloudFront)       │   │  Admin UI  (S3 + CloudFront)       │
              │  React + Cloudscape               │   │  React + Cloudscape                │
              │  Cognito login (app pool)         │   │  Cognito login (admin pool)        │
              └──────────────────────┬────────────┘   └────────────────┬──────────────────┘
                                     │ POST /chat + JWT                 │ REST + JWT (/flags, /benchmarks)
                                     ▼                                  ▼
              ┌───────────────────────────────────┐   ┌────────────────────────────────────┐
              │  Chat API                          │   │  Control-Plane API                 │
              │  API Gateway + Lambda              │   │  API Gateway + Lambda              │
              │  Cognito authorizer (app pool)     │   │  Cognito authorizer (admin pool)   │
              │  (chat_api/handler.py)             │   │  (lambda_entry.py)                 │
              └──────────────────────┬─────────────┘   └───┬───────────────┬────────────────┘
                                     │ InvokeAgentRuntime       │ R/W flags     │ R/W runs + load-gen
                                     │ (identity from JWT)       │               │
   ═══════════ APPLICATION PLANE ════│══════════════════        │               │
                                     ▼                          │               │
              ┌───────────────────────────────────┐            │               │
              │  AgentCore Runtime                 │            │               │
              │  (Strands, ARM64 container)        │            │               │
              │                                    │            │               │
              │   ┌───────────────────────────┐    │            │               │
              │   │      ORCHESTRATOR         │    │  short-term│               │
              │   │      (Sonnet)             │◄───┼── memory ──┼─┐             │
              │   └───────┬───────────┬───────┘    │ (flag-gated)│             │
              │   agents- │           │ -as-tools  │            │ │             │
              │           ▼           ▼            │            │ │             │
              │   ┌────────────┐ ┌────────────┐    │            │ │             │
              │   │ KNOWLEDGE  │ │   ORDER     │    │            │ │             │
              │   │  (Haiku)   │ │  (Haiku)    │    │            │ │             │
              │   └─────┬──────┘ └─────┬──────┘    │            │ │             │
              │ search_kb│    get_orders,          │            │ │             │
              │          │    lookup_customer      │            │ │             │
              └──────────┼──────────────┼──────────┘            │ │             │
                         │              │                       │ │             │
             reads Faq   │   reads      │ reads Orders          │ │ writes/reads│
                         │              │ (keyed by user_id=sub)│ │             │
                         ▼              ▼                       ▼ ▼             ▼
              ┌─────────────┐  ┌──────────────────────┐  ┌─────────────┐  ┌──────────────────┐
              │  Faq        │  │ Orders (DynamoDB)    │  │  Flags       │  │ BenchmarkRuns    │
              │ (DynamoDB)  │  │  app-plane owned     │  │ (SHARED)     │  │ (DynamoDB)       │
              │ app-plane   │  │  keyed by Cognito sub│  │ ctrl writes, │  │ control owned    │
              └─────────────┘  └──────────────────────┘  │ agent reads  │  └──────────────────┘
                         │                                └─────────────┘         ▲
   AgentCore Memory ◄────┤  (short-term conversation events, scoped by            │
   (AWS::BedrockAgentCore│   actor_id=user_id + session_id; flag-gated)           │
    ::Memory, id via env)│                                                        │
              Bedrock models ──┘                                                  │
              (Sonnet / Haiku / Nova via inference profiles)     Load generator ──┘
                                                                 (replays scenarios/v1.json
              Observability: AgentCore → CloudWatch Logs          through the agent path,
              (per-turn token usage returned in the response)     tagged with run_id)

              Customer identity + profile (name, email, custom:tier) live in the
              Cognito app-plane user pool — there is NO Customers table.
```

**Stacks (CDK):** `Tokenomics-Data` (shared Flags table), `Tokenomics-AppPlane`
(agent runtime, AgentCore Memory, Orders/Faq tables, app Cognito pool, Chat API,
Chat UI), `Tokenomics-ControlPlane` (admin Cognito pool, admin API, admin UI,
BenchmarkRuns). The control-plane stack references the runtime ARN from the
app-plane stack via a CDK cross-stack reference, so deploy order is app → control.

---

## Authentication

Both planes are gated by **Amazon Cognito**, with a **separate user pool per
audience**:

| Plane | Pool | Self sign-up | Identity used |
|-------|------|--------------|---------------|
| App (customer chat) | `Tokenomics-app-plane` | Off (see note) — customers provisioned via seed | Cognito `sub` = canonical `user_id`; profile (name/email/`custom:tier`) in the pool |
| Control (admin) | `Tokenomics-control-plane` | Off — operators provisioned | Admin JWT; authorizer gates the control API |

Both APIs sit behind a Cognito authorizer; unauthenticated requests get `401`.
The customer's identity comes from the **verified JWT claims** (never the request
body): `sub` (→ `user_id`, keys Orders + the per-conversation session), `email`,
`custom:tier`. CORS is scoped to the CloudFront origins.

> Note: customer self sign-up is temporarily disabled at the pool level (AWS's
> automated abuse mitigation force-disabled open Cognito self sign-up on this
> account). The register flow remains in code behind a flag.

---

## Conversation memory (short-term)

Memory is a **flag-gated tokenomics lever** (`memory` on/off), **orchestrator-only**.

- A single **`AWS::BedrockAgentCore::Memory`** resource (CDK L1 `CfnMemory`,
  short-term only — no long-term strategies in V1) is provisioned in the
  app-plane stack; its id is injected into the runtime as `AGENTCORE_MEMORY_ID`.
- When `memory` is ON, the orchestrator is built with a Strands
  `AgentCoreMemorySessionManager` scoped by `(actor_id=user_id, session_id)`, so
  it recalls earlier turns in the conversation. A stable orchestrator `agent_id`
  ensures restore matches across turns. OFF → stateless (baseline behaviour).
- Memory increases per-turn input tokens (history is re-sent) — the cost the
  other levers act on; prompt-cache offsets it. Specialists stay stateless.
- Benchmarks stay isolated via a unique `session_id` per run.

---

## Request flow 1 — Customer chat (per turn)

1. **Customer** logs in via **Cognito** in the **Chat UI** (CloudFront) and sends
   a message. The SPA `POST`s `{ message, conversation_id }` to the **Chat API**
   with the Cognito **JWT** (URL baked in at build time via `VITE_API_BASE`).
2. **API Gateway Cognito authorizer** validates the JWT and injects the verified
   claims. **Chat API Lambda** (`chat_api/handler.py`) reads identity from the
   claims (`sub`, `email`, `custom:tier`) — never from the body — builds a stable
   runtime session id from `sub` + `conversation_id`, and calls
   **`bedrock-agentcore:InvokeAgentRuntime`**.
3. **AgentCore Runtime** invokes the container entrypoint (`@app.entrypoint`),
   which builds a `RunContext` (`user_id`=sub, profile, session id) and calls
   `handle_turn`.
4. **Flag read.** `handle_turn` calls `tokenomics.flags.current()` → scans the
   shared **Flags** table (2-second in-process TTL cache) → yields a `FlagConfig`.
5. **Agent build (flag-aware).** The factory builds each agent with its own model
   from config. If `prompt_cache` is ON, the `BedrockModel` is built with
   `CacheConfig`. If `memory` is ON (and a memory resource is configured), the
   orchestrator is built with an `AgentCoreMemorySessionManager` (stable
   `agent_id`) so prior turns are restored; OFF → stateless.
6. **Orchestrate + delegate.** The **orchestrator** (Sonnet) classifies the
   request and, via in-process *agents-as-tools*, calls:
   - `ask_knowledge(...)` → **Knowledge** specialist → `search_kb` (Faq table)
   - `ask_order(...)` → **Order** specialist → `lookup_customer` / `get_orders`
     (Orders table, keyed by the signed-in user's `sub`; profile from JWT claims)
7. **Cost capture.** Each model call's Bedrock `usage` (input/output/cacheRead/
   cacheWrite tokens) is recorded by the `UsageCollector`, priced per-model from
   `price_map.py`, kept **both** as a turn total **and** a per-agent breakdown.
8. **Response.** The entrypoint returns
   `{ reply, usage (incl. by_agent), identity (incl. flag_config), model_ids }`.
   The Chat API relays it; the UI shows the reply, the cost line + per-agent
   breakdown, and the **active-levers indicator** (`memory ON/OFF · cache ON/OFF`)
   parsed from `identity.flag_config`.
9. **Observability.** AgentCore streams logs to CloudWatch; token/cost telemetry
   for the turn is returned inline in the response.

## Request flow 2 — Admin toggles a feature flag

1. **Admin** logs in (admin Cognito pool) and flips `prompt_cache` or `memory` in
   the **Admin UI** → `PUT /flags` (with JWT) to the **Control-Plane API**.
2. **Lambda** (`handlers/flags.py`) merges the change and writes one item per
   feature to the shared **Flags** table (generic over `ALL_FEATURES`).
3. On the **next turn/session**, the agent's flag reader picks it up after its 2s
   TTL — no redeploy, no restart. Construction-time levers (caching, memory,
   per-agent model) apply on the next agent build.

## Request flow 3 — Before/after benchmark (A/B)

1. **Admin** clicks *Run A/B* → `POST /benchmarks` (baseline, flags off), then a
   second run (e.g. prompt-cache on).
2. **Control-Plane Lambda** (`handlers/benchmarks.py`) writes a `RUNNING` record
   to **BenchmarkRuns**, then runs the **load generator** on a background thread.
3. The **load generator** (`handlers/load_generator.py`) replays the fixed
   `scenarios/v1.json` set concurrently, calling the agent via
   `InvokeAgentRuntime` with the run's flag config **pinned per request** (so a
   run never touches the live Flags table) and a unique `session_id` per run
   (`{run_id}-{index}`), tagging each with `run_id`.
4. Per-conversation `usage` is aggregated (`lib/metrics.py`) into run metrics; the
   run is marked `COMPLETED`.
5. The UI polls `GET /benchmarks/{id}` until both runs complete, then
   `GET /benchmarks/compare?a=&b=` renders the A/B (cost saved %, cache-hit delta,
   cost per conversation).

> Note: today's A/B toggles `prompt_cache` over single-message scenarios, so it
> does not exercise memory (single turns have nothing to recall). Multi-turn
> benchmark scenarios + a memory A/B are planned — see the memory feature spec V2.

---

## Tech stack

**Application plane**
- Agent framework: **Strands Agents SDK** (Python), orchestrator + 2 specialists (agents-as-tools)
- Hosting: **Amazon Bedrock AgentCore Runtime** (ARM64 container, `BedrockAgentCoreApp`)
- Models: **Amazon Bedrock** — Claude Sonnet 5.5 (global CRIS) / Claude Haiku 5.5 / Amazon Nova Lite (per-agent, via inference profiles)
- Auth: **Amazon Cognito** (app-plane user pool; `custom:tier`; profile store — no Customers table)
- Conversation memory: **Amazon Bedrock AgentCore Memory** (short-term, flag-gated, orchestrator-only)
- Chat API: **API Gateway (REST) + AWS Lambda** (JWT-authorized `InvokeAgentRuntime` proxy)
- Business data: **Amazon DynamoDB** (Orders keyed by Cognito `sub`, Faq)
- Chat UI: **React + Cloudscape** on **Amazon S3 + CloudFront**
- Observability: **AgentCore → Amazon CloudWatch Logs** (+ inline per-turn token/cost)

**Control plane**
- Auth: **Amazon Cognito** (dedicated admin user pool, self sign-up off)
- API: **API Gateway (REST) + AWS Lambda** (`/flags`, `/benchmarks`), Cognito authorizer
- State: **Amazon DynamoDB** (BenchmarkRuns) + shared **Flags** table
- Admin UI: **React + Cloudscape** on **Amazon S3 + CloudFront**
- Benchmark load generator: in-Lambda concurrent scenario replay

**Shared / platform**
- IaC: **AWS CDK** (Python) — `aws_bedrockagentcore.Runtime` L2 builds the agent image (ECR) + runtime; `CfnMemory` L1 for memory
- Cost engine: shared token→dollar `price_map` (per-model, per-bucket)
- Feature flags: shared DynamoDB `Flags` table + `flags_schema` contract (generic over `ALL_FEATURES`: `prompt_cache`, `memory`)

---

## Relationship to the design specs

- **This doc** = current as-built (living).
- [`DEVELOPER_GUIDE.md`](../../DEVELOPER_GUIDE.md) = code-level walkthrough.
- [`docs/optimisation-patterns/`](../optimisation-patterns/) = deeper reference per pattern.
