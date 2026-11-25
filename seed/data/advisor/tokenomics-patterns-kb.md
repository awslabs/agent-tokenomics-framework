# Tokenomics Patterns Knowledge Base

Tokenomics is the discipline of managing the token economics of an agentic AI system end to end,
across three pillars: **optimisation** (reduce cost on the average run), **governance** (bound and
control cost so the worst run can't blow the budget), and **observability** (make cost visible and
prove a change helped — cost and quality together).

This knowledge base is a catalog of cost patterns for a multi-agent customer-support system (a Sonnet
orchestrator that delegates in-process to Haiku/Nova specialists, with prompt caching and short-term
conversation memory as toggleable patterns). Each pattern describes a cost problem, the signals that
indicate it, the recommended fix, and the quality tradeoff. Use a pattern by matching an observed
signal in the usage data, then giving the recommendation with its expected effect and quality risk,
citing the pattern `id`.

**Reading a pattern:**
- **signal** — what to look for in the usage/cost data.
- **threshold** — a rule-of-thumb trigger. Rules-of-thumb, not hard limits; validate per workload.
- **recommendation** — the fix to advise.
- **expected_effect** — the direction and shape of the cost impact.
- **quality_risk** — low / medium / high, with why.
- **applies_to** — the scope the finding attaches to: model, agent, user, session, prompt, or turn.

---

## Key cost concepts

**Why agentic cost is different.** A single user request can trigger many model calls, tool
invocations, memory retrievals, and inter-agent handoffs — each one spends tokens. Cost is driven by
*runtime behaviour* (reasoning loops, delegation fan-out, growing context, tool storms), not by request
volume, so traditional request-response optimisation misses most of it.

**The four token buckets.** Every model call is billed across four disjoint buckets:
`input`, `output`, `cache_read`, and `cache_write`. Output tokens are the most expensive to generate.
Cached tokens are still billed: a cache *write* costs more than a plain input token (roughly 1.25× for
a short cache), while a cache *read* costs far less (on the order of a tenth of an input token). Total
token consumption therefore means `input + output + cache_read + cache_write`.

**Average vs worst run.** "Costs too much" hides two problems: the *average* run is too expensive
(an optimisation problem) and the *worst* run is unbounded (a governance problem). They need different
fixes — optimisation lowers the typical bill; governance caps the tail.

**Savings must be shown with quality.** Many governance limits "save" money by not finishing work, and
some optimisations can subtly degrade answers. A saving is only meaningful reported alongside its
quality or completion effect — never cost alone.

**Attribution is the prerequisite.** You cannot optimise, bill, or rate-limit what you cannot see.
Cost attributed per model, per agent, per user, and per session is what turns "the bill went up" into
"this agent, for this user, on this model" — the starting point for every other pattern.

---

## Optimisation patterns

### TKN-01 — Context management
- **pillar:** Optimisation
- **problem:** In a multi-turn conversation the full history is re-sent on every turn, so input tokens
  and cost climb turn over turn. Unbounded context growth is one of the largest cost drivers in a
  multi-turn agent.
- **signal:** Per-turn input tokens rising across a session; input dominating the token mix;
  conversation memory in use without any trimming or summarisation.
- **threshold:** Input is more than ~90% of total tokens on a session whose per-turn cost keeps rising.
- **recommendation:** Bound the replayed history — use a sliding window (drop or mask the oldest
  turns) or selective summarisation (compress old turns into a short summary) so context stops growing
  without limit.
- **expected_effect:** Flattens per-turn input growth; cost stabilises across a long conversation
  instead of compounding each turn.
- **quality_risk:** Medium — trimming too aggressively drops context a later turn needs; prefer
  summarising over hard truncation for recall-sensitive conversations.
- **applies_to:** session, agent, turn

### TKN-02 — Model routing by step complexity
- **pillar:** Optimisation
- **problem:** Paying frontier-model rates for trivial steps. A simple classification, routing, or
  extraction step does not need the most capable (most expensive) model.
- **signal:** One expensive model accounts for most spend while a cheaper model is already in use for
  other steps; an agent doing simple work on a frontier model.
- **threshold:** A single model is more than ~70% of spend while a cheaper model could handle part of
  that work.
- **recommendation:** Route each step to a model tier matched to its complexity (frontier for hard
  reasoning, mid/small for simple steps). Default to the capable model when step complexity is unclear.
- **expected_effect:** Lower blended cost per turn; the largest wins come from high-volume simple steps.
- **quality_risk:** Medium — routing a genuinely hard step to a weak model degrades answers; keep the
  capable model as the safe fallback.
- **applies_to:** agent, model

### TKN-03 — Prompt-cache placement
- **pillar:** Optimisation
- **problem:** The large, stable prefix of every request (system prompt plus tool descriptions) is
  reprocessed on every call. It is the most repeated cost in a multi-turn agent, and prompt caching can
  cut input-token cost on that prefix dramatically.
- **signal:** Low cache hit rate or a poor cache read-to-write ratio despite a stable prefix; a large
  system prompt with caching off; cache hits that regressed after a prompt edit.
- **threshold:** Cache hit rate below ~50%, or cache read:write below ~3:1.
- **recommendation:** Mark a cache breakpoint where the static content ends — after the system prompt
  and tool descriptions — and keep all volatile/dynamic content after it. Any dynamic content mixed
  into the prefix invalidates the cache. Confirm reuse via cache-read token counts.
- **expected_effect:** Large reduction on the repeated prefix cost; the saving grows with the number of
  turns and the length of the stable prefix.
- **quality_risk:** Low — caching does not change the model's output. The main hazard is silent
  regression: a one-line prefix change can reset the hit rate and quietly multiply spend.
- **applies_to:** agent, prompt

### TKN-04 — System-prompt optimisation
- **pillar:** Optimisation
- **problem:** Every token in the system prompt is paid for on every invocation, so prompt bloat
  compounds with traffic. Unstable ordering also breaks cache reuse.
- **signal:** A large system prompt; an input-heavy token mix; low cache hit rate traced to prompt
  churn.
- **threshold:** Input dominates the token mix and the static prompt is large relative to the user's
  actual message.
- **recommendation:** Compress the system prompt and tool descriptions to what the decision actually
  needs, put stable content first (byte-stable so it caches), and keep it versioned so the static part
  stays identical across calls.
- **expected_effect:** A smaller fixed cost per request that compounds across every turn and session;
  also improves cache hit rate (supports TKN-03).
- **quality_risk:** Low–medium — cutting necessary instructions can shift behaviour; change, then
  re-measure quality.
- **applies_to:** agent, prompt

### TKN-05 — Conversation memory as a managed cost pattern
- **pillar:** Optimisation pattern
- **problem:** Short-term conversation memory is not waste to remove — it is a cost *source* to manage.
  Replaying prior turns grows per-turn input tokens, which is exactly the cost that context management
  and prompt caching then act on.
- **signal:** Memory enabled with per-turn input tokens rising across the conversation; cost growth not
  offset by caching.
- **recommendation:** Keep memory where multi-turn recall matters, but pair it with prompt caching
  (TKN-03) to reclaim the replayed-history cost and with context management (TKN-01) to bound the
  growth. Scope memory to the orchestrator so specialists stay stateless.
- **expected_effect:** Memory alone raises cost; memory plus caching reclaims most of the increase while
  preserving recall.
- **quality_risk:** Low — memory improves answer quality by giving the agent recall; the cost is the
  tradeoff, not the quality.
- **applies_to:** session, agent, turn

### TKN-06 — Intra-session tool-result caching
- **pillar:** Optimisation
- **problem:** Agents re-call the same tool with identical arguments within a session, paying twice for
  the same answer plus an extra model round-trip to process it.
- **signal:** Repeated identical tool calls within a session; redundant lookups by a tool-using
  specialist.
- **threshold:** The same tool is called with the same arguments more than once in a session.
- **recommendation:** Cache each tool result within the session keyed by the tool name and arguments,
  and return the cached value on an exact repeat.
- **expected_effect:** Removes redundant tool round-trips and the model cost of re-reading them; helps
  verbose-tool agents most.
- **quality_risk:** Low — only exact-argument repeats are served from cache, and staleness is bounded to
  the session.
- **applies_to:** session, agent

---

## Governance patterns

### TKN-07 — Per-agent output-token cap
- **pillar:** Governance
- **problem:** Output tokens are the priciest to generate, and an agent can emit far more than a step
  actually needs.
- **signal:** A high output-token share for an agent; long replies on internal steps whose output is
  consumed by machines, not shown to the user.
- **recommendation:** Set a per-agent cap on output tokens per model call, sized to the step's real
  need (small for extraction, larger for planning).
- **expected_effect:** Lower, more predictable output cost per call; the biggest effect is on verbose
  agents.
- **quality_risk:** Medium — a cap set too low truncates a usable answer; size it to the step.
- **applies_to:** agent, model

### TKN-08 — Per-agent tool-call cap
- **pillar:** Governance
- **problem:** Tool-invocation storms — an agent loops tool calls within a single turn, each one adding
  model and tool cost. A known agentic cost-escalation pattern.
- **signal:** High tool-call counts within a single turn for one agent; retry storms on failed tool
  calls.
- **recommendation:** Cap the number of tool calls per turn per agent. The answer still returns; extra
  lookups are skipped (a soft limit).
- **expected_effect:** Bounds the worst-case turn cost caused by redundant or runaway tool use.
- **quality_risk:** Medium — a genuinely complex request may need more lookups; set the cap by agent
  role.
- **applies_to:** agent, turn

### TKN-09 — Per-agent iteration cap
- **pillar:** Governance
- **problem:** Reasoning loops that continue until they happen to exit or time out — a top cause of cost
  blowouts. The loop keeps spending tokens without converging.
- **signal:** Runaway event-loop iteration counts in an agent call; a turn far more expensive than its
  peers for similar work.
- **recommendation:** Set a bounded iteration limit per agent call and stop early with the best answer
  so far; combine with confidence-based early termination where possible.
- **expected_effect:** Converts an open-ended loop risk into a known ceiling.
- **quality_risk:** Medium — stopping early can yield a partial answer; set the limit above normal
  convergence for the task.
- **applies_to:** agent, turn

### TKN-10 — Per-agent total-token cap
- **pillar:** Governance
- **problem:** A single agent invocation can accumulate input and output across many internal model
  calls with no overall bound.
- **signal:** High total tokens within one agent call; one agent dominating a turn's cost.
- **recommendation:** Cap the input-plus-output tokens summed across a single agent invocation.
- **expected_effect:** Bounds per-agent-call spend regardless of how many internal model calls it makes.
- **quality_risk:** Medium — an early stop on a legitimately large task; size per agent.
- **applies_to:** agent, turn

### TKN-11 — Per-session token cap
- **pillar:** Governance
- **problem:** One conversation grows without bound across turns, made worse by memory re-sending
  history.
- **signal:** Session lifetime tokens approaching or exceeding a sensible ceiling; a steep per-turn
  growth curve within a session.
- **recommendation:** Cap cumulative tokens per conversation and prompt the user to start a new
  conversation when the limit is reached. The limit resets with each new session.
- **expected_effect:** Bounds the cost of a single long conversation.
- **quality_risk:** Medium — a hard stop ends the conversation; this is a user-experience guard, so set
  it generously.
- **applies_to:** session

### TKN-12 — Per-user quota (daily / monthly)
- **pillar:** Governance
- **problem:** A single user, or a few, drive a disproportionate share of spend with no per-user
  ceiling. In a multi-tenant system a handful of users often dominate cost.
- **signal:** A user far above peers on daily or monthly spend; heavy concentration at the top of the
  per-user ranking.
- **threshold:** A user's daily spend is more than ~10× the median across users.
- **recommendation:** Set a per-user daily and monthly token quota (counted per user). Block turns once
  a user is over quota, and fail closed (block on counter error) because it is a money cap.
- **expected_effect:** Caps worst-case per-user spend and protects the account from a single runaway
  tenant.
- **quality_risk:** Medium — a blocked user gets a hard "limit reached"; size the quota to legitimate
  heavy use, and consider tiering.
- **applies_to:** user

### TKN-13 — Per-user rate throttle
- **pillar:** Governance
- **problem:** A burst of requests from one user spikes cost in seconds — faster than any billing alarm
  can react.
- **signal:** High tokens-per-minute for a single user; throttle events in the record.
- **recommendation:** Set a per-user tokens-per-minute ceiling over a rolling-minute window; throttle
  over-rate turns with a short retry hint rather than denying outright.
- **expected_effect:** Smooths spikes and bounds the short-window burn rate per user.
- **quality_risk:** Low — a transient retry, not a denial; set it above a normal interactive pace.
- **applies_to:** user

### TKN-14 — Checkpoints and retries
- **pillar:** Governance
- **problem:** A late-stage failure in a multi-agent pipeline re-runs the whole pipeline, re-paying for
  every token already spent.
- **signal:** Repeated failures or retries on multi-step runs; cost spent re-executing work that had
  already succeeded.
- **recommendation:** Persist a checkpoint of run state after each agent or step (keyed by a stable
  transaction id) and resume from the last good checkpoint on failure instead of starting over.
- **expected_effect:** Turns a full re-run into a cheap continuation and caps the cost of transient
  failures.
- **quality_risk:** Low — resuming from a checkpoint does not change the output.
- **applies_to:** session, turn

### TKN-15 — Output controls (dynamic max tokens, conciseness, structured output)
- **pillar:** Governance
- **problem:** Internal, machine-consumed steps (tool selection, verification, routing) emit verbose
  prose the user never sees — pure cost with no user value.
- **signal:** High output tokens on internal steps; verbose free-form text from steps that only feed
  other steps.
- **recommendation:** Cap output length per step type, instruct internal steps to respond concisely,
  and require compact structured (JSON) output for machine-consumed steps instead of prose.
- **expected_effect:** Cuts wasted output tokens on internal steps with little user-visible change.
- **quality_risk:** Low–medium — over-tight caps can truncate a needed internal result; tune per step.
- **applies_to:** agent, turn

### TKN-16 — Circuit breaker and graceful degradation
- **pillar:** Governance
- **problem:** A single degenerate loop can multiply a bill in seconds, and a hard crash wastes the
  whole run.
- **signal:** A step costing far more than the running average; a turn spiking relative to its session.
- **recommendation:** Detect a step far above the average and pause or degrade — switch to a cheaper
  model, skip optional verification — and at the ceiling return the best answer so far instead of
  crashing.
- **expected_effect:** Caps the blast radius of a runaway step; a degraded answer beats a blown budget
  or a crash.
- **quality_risk:** Medium — degradation lowers answer quality by design, so surface the tradeoff.
- **applies_to:** agent, turn, session

---

## Observability patterns

### TKN-17 — Cost attribution and high-usage identification
- **pillar:** Observability
- **problem:** You cannot optimise, bill, or rate-limit what you cannot see per agent and per user, and
  a few users often drive most spend.
- **signal:** Available wherever per-agent and per-user cost roll-ups exist; outliers in the per-agent
  or per-user rankings.
- **threshold:** A user or agent whose daily spend is more than ~10× the median across its peers; a
  session whose tokens-per-turn is more than ~5× the session average.
- **recommendation:** Attribute dollarised cost per agent and per user, rank the heaviest consumers,
  and flag outliers for a closer look — then apply the matching governance or routing pattern.
- **expected_effect:** Enables targeted action. It produces no saving by itself; it points at where the
  saving is.
- **quality_risk:** Low — read-only observability.
- **applies_to:** agent, user

### TKN-18 — Prompt-cache hit-rate and savings observability
- **pillar:** Observability
- **problem:** Caching is the single biggest cost pattern but it is silent — a prefix edit can halve the
  hit rate and multiply spend, and the team often finds out only from the invoice.
- **signal:** A falling cache hit rate or read-to-write ratio over time; a regression right after a
  prompt or tool-description change.
- **threshold:** Cache hit rate below ~50%, read:write below ~3:1, or a clear drop versus the prior
  period.
- **recommendation:** Track per-agent cache hit ratio and dollars saved versus a no-cache baseline,
  alert on regression, and revisit prompt-cache placement and system-prompt stability (TKN-03, TKN-04).
- **expected_effect:** Protects an existing saving by catching silent regressions early.
- **quality_risk:** Low — observability.
- **applies_to:** agent, prompt, model

### TKN-19 — Real-time anomaly and runaway-loop detection
- **pillar:** Observability
- **problem:** Billing-based cost anomaly detection can lag by up to a day; by the time it fires, the
  loop has already finished and burned the budget. Agent cost escalations (reasoning-loop spikes, tool
  storms, memory growth) need to be caught within minutes.
- **signal:** Daily spend far above the recent median; a turn or session spiking relative to its own
  history.
- **threshold:** Daily spend more than ~3× the trailing 14-day median; session tokens-per-turn more
  than ~5× the session average.
- **recommendation:** Flag the spike in near-real-time and point at the likely cause — a reasoning loop
  (apply TKN-09), a tool storm (TKN-08), or misrouting (TKN-02) — so the owning team can act while it is
  still happening.
- **expected_effect:** Catches a runaway while it is in progress instead of after the bill arrives.
- **quality_risk:** Low — detection and alerting.
- **applies_to:** user, agent, session, turn

### TKN-20 — Cost-per-outcome (unit economics)
- **pillar:** Observability
- **problem:** Cost per token or per session is meaningless to a business without an outcome
  denominator; the decision-making number is cost per successful outcome.
- **signal:** A per-conversation outcome label (for example, resolved versus escalated) is required to
  compute this. Without one, the metric cannot be reported honestly.
- **recommendation:** Report cost per resolved request / successful task once an outcome label exists.
  Until then, state the gap rather than estimating a figure.
- **expected_effect:** Reframes every saving against business value; it produces no saving by itself.
- **quality_risk:** Low — observability.
- **applies_to:** session, agent

---

## Applying these patterns together

- **Prioritise by impact, risk, and effort.** Rank candidate fixes by dollar impact, then quality
  risk, then effort. Prompt caching and system-prompt optimisation are usually the highest-impact,
  lowest-risk starting points; model routing and context management are next; governance caps protect
  the tail.
- **Optimisation and governance are complementary.** Optimisation lowers the average run; governance
  bounds the worst run. A system usually needs both.
- **Patterns interact.** Memory raises per-turn cost; caching and context management reclaim and bound it.
  Routing lowers blended cost but can raise quality risk. Always consider the combination, not one
  pattern in isolation.
- **Prove every change with cost and quality together.** Report the cost delta alongside a quality or
  completion signal so a saving is never presented without its tradeoff.
