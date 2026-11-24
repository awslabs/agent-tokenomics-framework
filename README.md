# Agent Tokenomics Framework

**Agentic token-economics best practices, demonstrated end to end on a realistic AWS workload — with cost and quality measured before and after each technique.**

A runnable AWS Sample built on **Strands Agents + Amazon Bedrock AgentCore**. It pairs a real multi-agent customer-support workload with a **Tokenomics control plane** that lets you toggle each optimization/governance technique on or off and prove the savings with A/B benchmarks.

---

## Why This Exists

Agentic AI burns far more tokens than chatbot workloads — agents re-send accumulated context every turn, delegate across agents, and loop, so cost grows fast and spikes are found after the fact. The problem has three parts:

- **What do I fix first?** — a prioritised set of techniques, shown working.
- **How do I keep it within limits?** — governance that caps and controls spend.
- **How do I know it worked?** — a way to measure the savings *and* the quality tradeoff.

This reference solution answers all three by managing token cost across its whole lifecycle — **optimisation, governance, observability** — and proving each change on a real workload rather than describing it on paper.

**Guiding rule — reuse, never duplicate.** We do not rebuild what AgentCore provides natively (logs, traces, native observability views, evaluations). We build only the delta the platform leaves out: dollarized cost, feature flags, usage plans, benchmark A/B, and high-usage ranking.

---

## The Two Planes

- **Application plane** — a **customer-support agent** (orchestrator + 2 specialists, Strands, hosted on AgentCore Runtime) standing in for a production workload. Deliberately complex: multi-turn loops, delegation, redundant tool lookups, and a large stable system prompt so every technique has something to act on.
- **Tokenomics control plane** — a serverless app (React + Cloudscape on S3/CloudFront, API Gateway + Lambda, DynamoDB) where an admin **sees cost**, **sets usage plans**, **toggles each tokenomics feature on/off**, and **runs before/after benchmarks**.

### Two audiences

| Audience | Interface | What they do |
|----------|-----------|--------------|
| **End customer** (e-commerce shopper) | Real chat UI | Signs in (Cognito), then sends free-form support questions; identity comes from the JWT (`sub`). |
| **Admin / operator** (business or AWS partner) | Control plane | Signs in to a separate admin pool; sets usage plans, toggles features, watches cost, runs benchmarks. |

Benchmarking is separate from the chat: real conversations happen in the chat UI; benchmarks are driven through the API by a load generator that simulates many bulk conversations.

---

## Authentication

Both planes are gated by **Amazon Cognito**, with a **separate user pool per audience** (customers and operators are distinct populations):

| Plane | Pool | Self sign-up | UI |
|-------|------|--------------|----|
| **App** (customer chat) | `Tokenomics-app-plane` | **Off (for now)** — customers are provisioned via the seed; tier (`prime`/`basic`) is stored in `custom:tier`. Self sign-up is temporarily disabled (see note) | Custom login SPA |
| **Control** (admin) | `Tokenomics-control-plane` | **Off** — operators are provisioned, not self-served | Custom login SPA (no register) |

Both APIs sit behind a Cognito authorizer; unauthenticated requests get `401`. The customer's identity (Cognito `sub`) is the canonical `user_id` — it keys their Orders and their per-conversation session. There is no separate customers table: the user profile (name, email, tier) lives in Cognito and is forwarded to the agent via JWT claims.

Seeded demo users are listed below. **Note:** customer self-registration is temporarily disabled — AWS's automated abuse mitigation force-disables open Cognito self sign-up on this account, so customers are provisioned via the seed rather than self-registering. The register flow remains in the code (behind a flag) and can be re-enabled once the pool has the protections AWS requires.

---

## Demo Users

Seeded automatically by the deploy scripts. **No passwords are committed, generated or printed.** Set the password variables below before you deploy. A user is created only when its variable is set; otherwise the seed skips that user and prints a warning naming the variable to set, so set it and re-run the deploy. Re-deploys never reset an existing user's password unless the variable is set (and when it is set, the existing user's password is changed to it). Pools require 12+ characters with upper, lower, digit and symbol.

| Pool | Users | Password variable | Required |
|------|-------|-------------------|----------|
| App plane (customers) | demo customers in `seed/data/users.json` (all share this password) | `TOKENOMICS_USER_PASSWORD` | Yes |
| Control plane (admins) | `admin@example.com` (`governance-admin`) | `TOKENOMICS_ADMIN_PASSWORD` | Yes |
| Control plane (admins) | optional read-only user from `--viewer-email` (`viewer`) | `TOKENOMICS_VIEWER_PASSWORD` | Only with `--viewer-email` |

```bash
export TOKENOMICS_ADMIN_PASSWORD='<your strong password>'
export TOKENOMICS_USER_PASSWORD='<your strong password>'
./scripts/deploy-all.sh
```

> **Upgrading an existing deployment:** older versions seeded fixed demo passwords that are publicly known. Re-deploy with the variables above set (or reset the passwords in Cognito), or delete the demo users.

Optional: enable Cognito threat protection (compromised-credential detection, adaptive authentication) on both pools by setting `"cognitoThreatProtection": true` under `context` in `infra/cdk.json` (or `-c cognitoThreatProtection=true` on a direct `cdk deploy`). It uses the Cognito PLUS feature plan, billed per monthly active user, so it is off by default. Recommended for any non-throwaway deployment.

### App plane — Chat UI (customers)

Pool `Tokenomics-app-plane`. Source: [`seed/data/users.json`](seed/data/users.json).

| Email | Tier | Has orders |
|-------|------|------------|
| `bob@example.com` | prime | Yes (2) |
| `eva@example.com` | basic | Yes (2) |

### Control plane — Admin UI (operators)

Pool `Tokenomics-control-plane` (self sign-up disabled — provisioned by the deploy script). Source: `seed/load_control_plane.py`.

| Email | Role |
|-------|------|
| `admin@example.com` | `governance-admin` — may change flags, usage plans and overrides, and run benchmarks |

Control-plane access is role-based (Cognito groups in the admin pool): `governance-admin` can read and change everything; `viewer` is read-only; a user in neither group gets `403`. Every change is recorded in an append-only audit trail (who, when, before/after), shown as **Change history** on the Governance page. To add a read-only user, set `TOKENOMICS_VIEWER_PASSWORD` and pass `--viewer-email` to `seed/load_control_plane.py`, or add the user to the `viewer` group in Cognito. A group change takes effect at the user's next sign-in.

---

## How It Works

Every technique is a **toggle-able overlay** on a working baseline agent. All strategy code ships in the deployed container, defaulting **off**; flipping a flag in the control plane changes behavior with **no redeploy** — hook-based features apply on the next request, and construction-based features (context management, model routing) apply on the next session.

```
End customer ──chat──► Support agent (Strands on AgentCore) ──telemetry──► CloudWatch
                              ▲                                                │
        flags / usage plans   │                                                ▼
   Admin ──► Control plane ────┘                              Control plane reads $ cost,
   (React + Cloudscape, API GW + Lambda, DynamoDB)            cache health, high-usage users
                              ▲
   Load generator ──bulk conversations via API (benchmark runs)
```

---

## V1 Features

The techniques prioritised for V1, across the three pillars (memory added after the original seven):

| # | Feature | Pillar |
|---|---------|--------|
| 1 | Context management (configure-native on Strands) ✅ | Optimisation |
| 2 | Model routing by step complexity (configure-native) | Optimisation |
| 3 | Prompt-cache placement | Optimisation |
| 4 | System-prompt optimisation | Optimisation |
| 5 | High-usage user identification | Observability |
| 6 | Checkpoints & retries (resume by `transaction_id`) | Governance |
| 7 | Per-agent/session/step budget limits (usage plans) | Governance |
| 8 | Conversation memory (short-term, AgentCore Memory) ✅ | Optimisation lever |

Supporting observability — cost attribution, prompt-cache hit-rate & savings, cost-per-outcome, real-time anomaly/runaway-loop detection — is built on AgentCore/CloudWatch telemetry (reuse-first).

**Conversation memory** is a toggle-able lever: with the `memory` flag on, the support agent recalls earlier turns in a conversation (short-term AgentCore Memory, orchestrator-scoped). It makes the chat production-like and, for tokenomics, adds the per-turn input-token growth that prompt-cache and context management then optimise. Long-term memory is planned for a later release.

**Context management** is a toggle-able lever that keeps the orchestrator's conversation history lean (configure Strands' native conversation managers, don't rebuild). It only has an effect with `memory` on. It is **masking-first**: when memory restores history, earlier turns' tool calls and specialist answers are replaced with short placeholders, keeping your questions and the agent's replies. On top of that it picks a mode from the prompt-cache flag: with caching **off**, a sliding window (default 12 messages, editable on the Flags page); with caching **on**, it summarises only when the prompt gets large (default 20,000 tokens, editable as "Max tokens before summarizing") using a cheap Haiku summarizer, because trimming every turn would break the history cache. Summarizer spend is counted inside the orchestrator (it is an internal component, not an agent) and broken out separately, and benchmarks report tool blocks masked, messages trimmed, summarizations, cache read/write cost and input tokens per turn. See the developer guide §3.6.

**Tokenomics Advisor** — a read-only admin chat on the control plane (a Strands agent on its own AgentCore Runtime) that explains what drives cost and which tokenomics patterns apply, citing only figures read from your metrics and configuration. See [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) §4.8.

---

## Benchmarking — Before / After Proof

Two modes over the same flags and the same load generator:

- **Live toggle + load** — flip a feature, fire bulk conversations, watch cost move in near-real-time.
- **Benchmark runs** — a fixed, versioned scenario set (same conversations, same seed, deterministic backends) is run as a named run tagged with the exact flag config, so **baseline (all flags off)** vs **optimized** can be compared on identical inputs. Savings are always shown *with* the quality tradeoff, never alone.

---

## Technology Stack

| Layer | Choice |
|-------|--------|
| Chat & control-plane frontends | React + Cloudscape Design System |
| Static hosting | Amazon S3 + CloudFront |
| Control-plane API | API Gateway + AWS Lambda |
| Config / state store | Amazon DynamoDB |
| Authentication | Amazon Cognito (separate pool per plane) |
| Agent framework | Strands Agents SDK (Python) |
| Agent hosting | Amazon Bedrock AgentCore Runtime |
| Conversation memory | Amazon Bedrock AgentCore Memory (short-term) |
| Inter-agent delegation | In-process (A2A-ready seam for future) |
| Models | Bedrock: Claude Sonnet / Claude Haiku / Amazon Nova Lite |
| Telemetry | AgentCore Observability → CloudWatch (reused) |
| IaC | AWS CDK |

---

## Project Structure

```
├── docs/                              # Architecture reference + optimisation patterns
├── src/
│   ├── app-plane/                     # Strands support agent (AgentCore Runtime)
│   │   ├── entrypoint.py              # BedrockAgentCoreApp @app.entrypoint
│   │   ├── Dockerfile                 # ARM64 image for AgentCore
│   │   ├── agents/ tools/ tokenomics/ # orchestrator+specialists, tools, levers
│   │   └── store.py                   # DynamoDB-backed business data
│   │   └── chat_api/                  # thin proxy Lambda the chat UI calls
│   ├── control-plane/                 # admin API (flags + benchmarks)
│   │   ├── lambda_entry.py            # API Gateway proxy -> handlers
│   │   ├── handlers/ lib/             # flags, benchmarks; DynamoDB store
│   │   └── scenarios/v1.json          # fixed benchmark scenario set
│   └── shared/                        # flags schema + token->$ price map
├── web/                               # React + Cloudscape SPAs (admin + chat)
├── infra/                             # CDK stacks + seed script + seed data
└── scripts/                           # deploy-all.sh (+ deploy-app-plane.sh, deploy-control.sh)
```

For a deep, code-level walkthrough of the design and implementation (the two planes, the agentic turn, the tokenomics patterns, and token governance), read the **[Developer Guide](DEVELOPER_GUIDE.md)**.

Start with the **[current architecture](docs/architecture/architecture.md)** — the living, as-built reference.

---

## Development Setup

### Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| **AWS CLI v2** | Deploy + query AWS; must be authenticated to the target account | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| **Python 3.12+** | Runs the CDK app (infra) and the seed step | [python.org](https://www.python.org/downloads/) |
| **Node.js 20.19+ or 22.12+** | Runs the AWS CDK CLI (`npx`) and builds the SPAs (Vite 8 requires these minimums) | [nodejs.org](https://nodejs.org/) |
| **Docker** (or Finch/Podman) | Builds the ARM64 agent container image | [docker.com](https://www.docker.com/) |

Also do two things yourself before deploying:
1. **Authenticate the AWS CLI** — `aws configure` (or set `AWS_PROFILE`).
2. **Enable Amazon Bedrock model access** for Claude Sonnet 5.5, Claude Haiku
   5.5, and Amazon Nova Lite **in the region you deploy to** — the agent fails
   at invoke time without it. Sonnet 5.5 is reached via the Global cross-Region
   inference profile. `deploy-all.sh` reminds you which region that is.

### Deploy

One command, run from the repo root — sets up the environment and deploys both
planes end to end:

```bash
bash scripts/deploy-all.sh          # prompts for region (default us-west-2)
```

It checks your tools, picks + saves the region, creates the CDK virtualenv,
installs the SPA deps, then runs the two deploy stages for you. When it finishes
it prints the chat UI, admin UI, and API URLs.

To run a stage on its own instead (e.g. redeploy just one plane), the region
from the first run is saved to `infra/.deploy-region` and reused:

```bash
bash scripts/deploy-app-plane.sh    # Data + AgentCore Runtime (agent image) + chat UI + seed
bash scripts/deploy-control.sh      # admin API (API Gateway + Lambda) + admin UI + seed
```

Each deploy script builds its SPA, deploys the CDK stacks, and prints its URLs
(chat UI, admin UI, APIs) when done. `deploy-all.sh` verifies each prerequisite
above and stops with a clear fix hint if one is missing or too
old; it never auto-installs system tools.

See the **[architecture](docs/architecture/architecture.md)** for stack-level details.

---

## Status

**Phase: V1 cloud build.** Fully deployed on AWS via CDK/CloudFormation:

- **App plane** — the Strands support agent on Amazon Bedrock AgentCore Runtime,
  its business data (Orders/Faq) in DynamoDB, customer identity + profile in a
  Cognito user pool, short-term conversation memory in AgentCore Memory (flag-
  gated), and the customer chat UI (S3/CloudFront) calling the agent through a
  thin, JWT-authorized app-plane chat API.
- **Control plane** — the admin API (API Gateway + Lambda, behind a dedicated
  admin Cognito pool) for feature flags and benchmarks, benchmark-run state in
  DynamoDB, and the admin UI (S3/CloudFront).
- **Data** — the shared feature-flags table (written by the control plane, read
  by the agent).

CDK owns every resource, including the AgentCore Runtime (built from the
Dockerfile via the `aws_bedrockagentcore.Runtime` construct).

---

## Testing

Unit tests (fast, offline — the default run):

```bash
src/app-plane/.venv/bin/python -m pytest -q
```

This includes the context-management unit tests (`tests/context_management/`):
flag schema, `auto` mode selection, summarizer cost attribution, session restore
across a mid-session flag change, count-preserving masking, the chat API's error
mapping, and the per-turn / cache-cost benchmark roll-ups — no model calls, no AWS.

Live token-governance suite (opt-in; runs against the **deployed** stack, reuses
the demo users, and restores your config afterward):

```bash
bash tests/governance/test-governance.sh              # everything
bash tests/governance/test-governance.sh analytics    # read-only label/graph checks (free)
bash tests/governance/test-governance.sh enforcement  # the 8 settings fire (small Bedrock cost)
```

Live benchmarking suite (opt-in; runs against the **deployed** stack). Starts a
real multi-turn A/B benchmark — 2 sessions × 3 turns, Run A = memory only, Run B
= memory + prompt caching — the agent answers all 12 turns, and the test prints
the A-vs-B comparison (cost, cache, gain) and asserts caching made Run B cheaper
with benchmark spend tagged `source=benchmark` (never pollutes live analytics):

```bash
bash tests/benchmarking/test-benchmarking.sh   # sends 12 short real agent turns (small Bedrock cost)
```

See [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) §5 for what each layer covers and the
prerequisites.

---

## Related AWS Resources

- [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
- [Amazon Bedrock Prompt Caching](https://aws.amazon.com/bedrock/prompt-caching/)
- [Strands Agents SDK](https://strandsagents.com)
- [AWS Well-Architected Agentic AI Lens — Cost Optimization](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/cost-optimization.html)
- [Cloudscape Design System](https://cloudscape.design/)
