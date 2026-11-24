# Model Routing by Step Complexity — Optimisation Pattern

Deeper technical reference for the `model_routing` lever. For the quick tour see
`DEVELOPER_GUIDE.md §3.6`.

---

## 1. The problem

A multi-agent assistant pays **frontier-model rates for every step, even trivial ones.**
The orchestrator (Claude Sonnet 5.5) runs the same expensive model whether it is doing genuine
multi-step reasoning or just greeting the customer and formatting a reply. Conversely a cheap
specialist (Claude Haiku 5.5) runs the same small model even on an occasional genuinely hard task.
Step complexity varies turn to turn; a fixed model per agent cannot track it.

This is **AWS Well-Architected Agentic AI Lens — AGENTCOST02-BP01** ("tiered model selection:
match each task to the cheapest model capable of acceptable quality, escalate only when confidence
drops; keep any pre-classifier cheaper than the tier price differential").

## 2. The lever

A flag-gated overlay (`model_routing`), OFF by default. ON → each agent runs behind a Strands
`ModelRouter` over two Claude candidates and a per-role strategy that picks the model by step
complexity. OFF → each agent's static model (the honest baseline). **Configure-native:** the SDK
does candidate resolution, per-invocation state, and the failover budget; we supply only the
routing *decision* and the cost-attribution plumbing.

| Agent | Default (candidate 0) | Classifier | Routes | On miss |
|-------|-----------------------|-----------|--------|---------|
| Orchestrator | Sonnet 5.5 (capable) | **LLM-as-judge** — Amazon Nova Lite | *down* to Haiku on a simple step | stays Sonnet (**fail safe**) |
| Knowledge | Haiku 5.5 (cheap) | **heuristic** (reasoning-keyword, no model call) | *up* to Sonnet on clear complexity | stays Haiku (**fail cheap**) |
| Order | Haiku 5.5 (cheap) | **heuristic** | *up* to Sonnet on clear complexity | stays Haiku (**fail cheap**) |

**Why two different classifiers.** The orchestrator defaults to the *expensive* model and its steps
vary a lot, so a tiny Nova judge easily costs less than the Sonnet→Haiku saving it unlocks (BP01's
pre-classifier rule). The specialists default to the *cheap* model and are single-shot, so adding
an LLM judge per step would cost more than the step itself — a deterministic keyword heuristic is
the right fit there.

**Candidate 0 = the agent's current static model**, so OFF and the ON-default run the *same* model.
Routing only ever moves *off* the baseline, which keeps the before/after A/B honest and sets the
fail direction per role (orchestrator fails to Sonnet; specialists fail to Haiku).

## 3. High-level architecture

```
  Customer ──HTTPS──► Chat UI (S3 + CloudFront) ──POST /chat──► Chat API (API GW + Lambda)
                                                                          │ InvokeAgentRuntime
  ═══════════════ APP PLANE — AgentCore Runtime (Strands) ════════════════╪═════════════════════
                                                                          ▼
  ┌──────────────────────────────────────────────────────────────────────────────────┐
  │  ORCHESTRATOR — ModelRouter                                                        │
  │  default Sonnet 5.5 (capable); route DOWN to Haiku on "simple";                    │
  │  miss / error → stay Sonnet (fail safe)                                            │
  │                                                                                    │
  │   ┌────────────────────────────────────────────────────────────────────────────┐ │
  │   │ routing strategy — LLM judge (Nova Lite): classifies each step SIMPLE/COMPLEX │ │
  │   │ INSIDE the orchestrator's own invocation (ModelRouter calls it — not a        │ │
  │   │ separate delegate). Governed by the per-user quota (ADR-001); metered as      │ │
  │   │ router_judge.                                                                 │ │
  │   └────────────────────────────────────────────────────────────────────────────┘ │
  └───────────────────────────┬────────────────────────────────┬───────────────────────┘
                ask_knowledge  │                                │  ask_order
                               ▼                                ▼
                    ┌──────────────────┐             ┌──────────────────┐
                    │ KNOWLEDGE        │             │ ORDER            │
                    │ ModelRouter      │             │ ModelRouter      │
                    │ default Haiku;   │             │ default Haiku;   │
                    │ escalate UP to   │             │ escalate UP to   │
                    │ Sonnet on a      │             │ Sonnet on a      │
                    │ reasoning keyword│             │ reasoning keyword│
                    └────────┬─────────┘             └────────┬─────────┘
                             │ all model calls                │
                             ▼                                ▼
         ┌──────────────────────────────────────────────────────────────────┐
         │  Amazon Bedrock    Sonnet 5.5 ⇄ Haiku 5.5 (agents) · Nova Lite (judge) │
         └──────────────────────────────────────────────────────────────────┘

  Tokenomics-Flags (DynamoDB): model_routing = on | off
     └─ read at construction → builds ModelRouters for ALL 3 agents (next turn; no redeploy)

  per turn: usage + model_routing block (routed_calls · capable/cheap · judge_cost · router_judge)
                              │  UsageRecorded (EventBridge)
                              ▼
       metering consumer (Lambda) ──► DynamoDB Metrics:  AGENT#router_judge#…   MODEL#<id>#…
```


## 4. The routing decision (`src/app-plane/tokenomics/routing.py`)

Both strategies implement the Strands `RoutingStrategy` protocol (`async select(context)`), return
a candidate from `context.candidates` to route, or `None` to decline (→ the default/candidate-0
model runs). Any exception is logged and collapsed to a decline, so a classifier hiccup never
breaks a turn — the fail direction is set by which model is candidate 0.

**Heuristic (specialists).** Escalates **only on an explicit reasoning signal** — "why", "compare",
"explain", "reconcile", "versus", "step by step", etc. It deliberately does **not** escalate on
order ids, dates, dollar amounts, or raw length: a specialist classifies the orchestrator's
composed task string, which routinely carries those even for trivial lookups, so those signals
would over-escalate routine work to Sonnet and erode the lever's savings. A genuine
cross-referencing task ("*compare* ORD-1001 and ORD-1002") carries a reasoning keyword and is
caught. When unsure, it fails cheap (stays on Haiku) — the accepted V1 specialist tradeoff.

**LLM-as-judge (orchestrator).** One Nova Lite call classifies the latest step SIMPLE or COMPLEX.
SIMPLE → route down to Haiku; anything else, or a parse/model error → decline → stay on Sonnet
(fail safe — never silently degrade a supervisory step on a classifier wobble). The judge:

- **Is stateless per classify** — its conversation history is cleared each call, so repeated
  classifies in a turn do not re-send prior exchanges (which would inflate input cost).
- **Is governed (ADR-001)** — it carries a per-user `QuotaHook`, so the daily/monthly quota and
  per-minute throttle gate it like any other model call; no model call, including a routing
  decision, escapes the money gate. It uses its *own* hook instance (sharing the turn's
  `GovernanceOutcome`) so its tiny per-step token projection does not skew the orchestrator's
  throttle decision.
- **Is metered** — each classify's own tokens are tallied and recorded as a dedicated
  `router_judge` cost bucket, so judge spend is visible and the grand total reconciles to the bill.

## 5. Cost attribution (the one real code change)

Strands routing applies at `InvokeModelStage`, so `agent.model` stays candidate 0 even when a
different candidate actually runs. Pricing an agent's usage at `agent.model` would therefore
mis-attribute a routed turn.

**Mechanism (verified against strands-agents 1.61.0).** The original intent — read the running
model *and* per-call usage off `AfterModelCallEvent` — does not work on this SDK: the event carries
no token usage, and `accumulated_usage` reads zero at hook time. The running model *id* is reachable
from `invocation_state` (`strands:model_routing…` key), so a `RoutingUsageTracker`
(`AfterModelCallEvent`) records the selected model per call, and at turn end each agent's
`accumulated_usage` is priced at the tracked model (fallback: the static id).

**Why this is exact.** Routing selects once per invocation and re-asks the strategy only after a
*failed* call; our strategies are not failure-aware (they re-classify to the same tier, which the
router treats as "candidate already used" → ends routing), so an invocation never splits across
two models. Same-model throttling retries accrue into the same bucket. A warning is logged if any
single agent ever reports more than one model in a turn (only possible with a failure-aware
strategy, which this feature does not ship).

## 6. Telemetry, metering & benchmarks

Each turn adds a `model_routing` block to the usage payload: `routed_calls` per model,
`capable_calls`/`cheap_calls` (tier counts labelled from the configured ids — not a model-family
substring, so an overridden model pair stays counted), `orchestrator_cheap_calls`,
`specialist_escalated_calls`, `judge_calls`, and `judge_cost_usd`. It rides inside the existing
`UsageRecorded` EventBridge event (no new event type, no new table). `lib/metrics.aggregate` rolls
these up per run and `handlers/benchmarks._feature_stats` renders: route distribution
(Capable % · Cheap %), orchestrator steps routed to cheap, specialist steps escalated, and judge
cost — alongside the baseline-vs-routing cost delta.

## 7. Configuration

| Flag | `model_routing` — on/off only in the UI |
|------|------------------------------------------|
| Params | `capable_model`, `cheap_model`, `judge_model` — blank ⇒ resolved from `config.py` (`ORCHESTRATOR_MODEL` / `KNOWLEDGE_MODEL` / `ROUTER_JUDGE_MODEL`), so the model ids live in one place |
| Applies | at agent construction — next session/turn; no redeploy |
| IAM | the Nova judge is covered by the Bedrock grant driven by `known_model_ids()` |

Two Claude tiers only (Sonnet 5.5 ⇄ Haiku 5.5), so routing is safe alongside the summarizing
context-management mode — proactive compression sizes against the default candidate's context
window (Sonnet 5.5 is 1M, Haiku 5.5 ~200k). Nova Lite is the judge only — never an agent's runtime model.

## 8. Quality tradeoff

Low–medium, not formally scored in V1. The orchestrator fails *safe* to Sonnet, so a judge miss
never silently degrades a supervisory step. A specialist heuristic false-negative keeps a hard step
on Haiku (a weaker answer); the conservative "escalate only on a clear reasoning signal" rule bounds
this. Savings are always reported with the route distribution so the tradeoff is visible, never cost
alone.

## 9. References

- AWS Well-Architected Agentic AI Lens — AGENTCOST02-BP01
- Strands `ModelRouter` / `RoutingStrategy` (configure-native)
