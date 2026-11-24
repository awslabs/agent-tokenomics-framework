# 5. Model routing by step complexity

**Pillar:** Optimization (configure native) · **Quality risk:** Low–medium — a misclassified hard step on a small model gives a weaker answer.

Aligns with AWS Well-Architected Agentic AI Lens **AGENTCOST02-BP01** (tiered model selection).

## Problem

A fixed model per agent pays frontier rates for trivial steps (greetings, formatting,
simple lookups), or uses a small model on the occasional genuinely hard step.

## Design rules

1. **Candidate 0 = the agent's current model.** Routing only moves *off* the baseline,
   so OFF and ON-default behave identically and the A/B is honest.
2. **Pick the fail direction per role:**
   - Expensive default (orchestrator/supervisor) → route **down** on "simple"; on
     doubt/error stay expensive (**fail safe**).
   - Cheap default (single-shot specialist) → escalate **up** on clear complexity; on
     doubt/error stay cheap (**fail cheap**).
3. **The classifier must cost less than the saving it unlocks.**
   - Expensive default: a tiny LLM judge (e.g. Nova Lite, 1-word output,
     `max_tokens≈16`, stateless per call) is fine.
   - Cheap default: use a deterministic heuristic (reasoning keywords like *why,
     compare, explain, reconcile, step by step*) — an LLM judge per step would cost more
     than the step.
4. **Classify the latest step, not the whole history.**
5. **Avoid noisy signals** like length, ids, dates, amounts — they over-escalate routine work.
6. **Attribute cost to the model that actually ran**, not `agent.model` (routing
   happens at invocation; the agent's model attribute still shows candidate 0).
7. **Meter the judge** as its own cost bucket so savings are net.
8. **Keep context windows compatible** across candidates (e.g. don't route a 500k-token
   context to a 200k-window model).

## Strands (configure, don't rebuild)

```python
from strands.models.routing import ModelRouter, RoutingCandidate

capable = RoutingCandidate(BedrockModel(model_id=SONNET, cache_config=cc), name="capable")
cheap   = RoutingCandidate(BedrockModel(model_id=HAIKU,  cache_config=cc), name="cheap")

class EscalateOnReasoning:                      # implements RoutingStrategy
    KEYWORDS = ("why", "compare", "explain", "reconcile", "versus", "step by step")
    async def select(self, context):
        text = latest_user_text(context).lower()
        if any(k in text for k in self.KEYWORDS):
            return next(c for c in context.candidates if c.name == "capable")
        return None                             # decline -> candidate 0 (cheap)

router = ModelRouter([cheap, capable], strategy=EscalateOnReasoning())
agent = Agent(model=router, ...)
```

Return a candidate **from `context.candidates`** (matched by identity) or `None` to
decline. Swallow exceptions into a decline so a classifier glitch never breaks a turn.

## Code smells to look for

- Every agent, including simple specialists, on the frontier model.
- A supervisor on a frontier model doing mostly greetings / formatting / hand-offs.
- An LLM judge in front of an already-cheap model.
- Cost computed from `agent.model` while routing is enabled.

## Verify

- Route distribution (% capable vs % cheap) alongside the cost delta — never report
  cost alone.
- Eval-set quality on the routed-down subset.
- Judge cost as a separate line item.
