"""Tokenomics runtime -- the flag-gated overlay on the baseline agent.

Guiding principle: "ship all code, gate on flags". Every tokenomics technique's
code lives here permanently and defaults OFF, so toggling a flag in the control
plane changes behaviour with no redeploy. The toggleable optimization techniques
are the "patterns": prompt-cache placement, short-term memory, context
management, and model routing. Governance (per-agent / session / per-user caps)
is enforced in-loop here too.

Pieces:
- flags.py          : read the live feature-flag config (short-TTL cached).
- plans.py          : read the live governance config (short-TTL cached).
- identity.py       : stamp agent/session/customer/run ids for attribution.
- cost.py           : turn Bedrock usage token counts into dollars + a running total.
- factory.py        : build the flag-aware agents (prompt-cache, context manager, router).
- routing.py        : the model-routing decision strategies.
- hooks/            : in-loop governance hooks (quota/throttle, tool-call cap).
- quota_counter.py / session_counter.py / _counter_store.py : persisted enforcement counters.
- metering.py       : emit per-turn usage/governance events to EventBridge.

---

Degradation policy (ONE rule, applied everywhere below)
--------------------------------------------------------
A tokenomics overlay must never break a customer turn. How a failure degrades
depends on what the failing piece protects:

1. FAIL-OPEN for UX guards + telemetry. If the piece only *measures* or is a
   soft UX guard (session-cap counter read, metering emit, memory build, context-
   management cost recording), a failure DEGRADES to baseline -- skip the guard /
   drop the event -- and the turn proceeds. A dropped increment self-heals on the
   next read.

2. FAIL-CLOSED for money limits. The per-user quota/throttle counter read is the
   only exception: it protects spend, so a read error BLOCKS the turn
   (`CounterUnavailable` -> the quota hook cancels the call). A money cap must
   never be bypassed by a store hiccup (ADR-001 integrity rule).

3. LOG BEFORE YOU DEGRADE. A fail-open path logs via the module's `logging` logger
   before returning the baseline, so a real outage is visible in CloudWatch and is
   never mistaken for "the feature is just off". A degraded *guard or outage* logs
   at WARNING (session-cap read, flag read, metering emit, memory build). The one
   exception is per-turn side-agent cost recording (context-management summarizer /
   recovery metering): a miss there only omits a small cost delta, runs on every
   turn, and would flood logs, so it logs at DEBUG. Use the stdlib `logging`
   module, not `print`. The root logger is configured once per runtime by
   `shared.logging_setup.configure_logging()` (called in each entrypoint, level
   from `LOG_LEVEL`), so INFO-and-above actually reach CloudWatch.
"""
