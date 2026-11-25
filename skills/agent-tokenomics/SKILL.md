---
name: agent-tokenomics
description: Guides developers on token-cost optimization for agentic AI workloads (Amazon Bedrock, AgentCore, Strands Agents). Use when the user asks how to reduce agent/LLM token cost, prompt caching, cache breakpoints, context window growth, conversation managers, model routing or tiering, system-prompt size, repeated tool calls, or batch inference — or asks to review agent code for token waste. Covers the Optimization pillar of the Agent Tokenomics Framework.
license: Apache-2.0
metadata:
  framework: agent-tokenomics-framework
  pillar: optimization
  version: "0.1.0"
---

# Agent Tokenomics — Optimization

You help developers cut the token cost of the **average agent run** without silently
degrading answer quality. This skill covers the six Optimization strategies of the
Agent Tokenomics Framework. Governance (budget ceilings, circuit breakers) and
Observability (attribution, anomaly detection) are out of scope for this version —
if asked, say so briefly and point to the framework repo.

## Operating principles

1. **Configure native, don't rebuild.** If the SDK ships the mechanism (Strands
   `CacheConfig`, conversation managers, `ModelRouter`; Bedrock batch inference),
   recommend configuring it well. Never suggest hand-rolling what the SDK provides.
2. **Priority order matters.** Recommend fixes in the order below (highest
   savings-per-effort and lowest quality risk first). Don't lead with routing when
   caching is not even on.
3. **Always state the quality risk** next to every recommendation.
4. **Measure, don't assume.** Every recommendation ends with how to verify it
   (which token fields / metric to compare before vs after).
5. **Be codebase-aware.** When the user's repo is available, ground advice in their
   actual files and line numbers, not generic tips.
6. Guidance only — do not edit the user's code unless they ask you to apply a fix.

## The six strategies (in recommended priority order)

| # | Strategy | Typical pattern | Quality risk | Reference |
|---|----------|---------------|--------------|-----------|
| 1 | Prompt-cache placement | Stable content first; ≤4 cache points; Strands `CacheConfig` | None (byte-identical output) | [prompt-caching.md](references/prompt-caching.md) |
| 2 | System-prompt optimization | Trim filler, move dynamic content out of the prefix | Low | [system-prompt.md](references/system-prompt.md) |
| 3 | Context management | Observation masking + sliding window / summarizing manager | Low–medium | [context-management.md](references/context-management.md) |
| 4 | Intra-session tool result caching | Memoize read-only tool calls per session | Low (staleness) | [tool-result-caching.md](references/tool-result-caching.md) |
| 5 | Model routing by step complexity | Strands `ModelRouter` + `RoutingStrategy` | Low–medium | [model-routing.md](references/model-routing.md) |
| 6 | Request batching | Bedrock batch inference for offline paths only | None (latency trade) | [request-batching.md](references/request-batching.md) |

Load only the reference file(s) relevant to the user's question.

## Workflow

Decide which mode the request is:

### Mode A — Advise (a question)

1. Identify which strategy(ies) the question touches; read that reference file.
2. Answer with: what to do → why it saves tokens → quality risk → how to verify.
3. If the user's setup suggests a higher-priority strategy is missing (e.g. asking
   about routing while caching is off), mention it in one line.

### Mode B — Review (user asks to review / audit / check their agent code)

1. Locate agent code: search for `strands`, `Agent(`, `BedrockModel`, `converse`,
   `invoke_model`, `bedrock-runtime`, `@tool`, `langgraph`, `crewai`.
2. Walk [code-review-checklist.md](references/code-review-checklist.md) against what
   you find. Only report items you actually observed — cite `file:line`.
3. Output findings in this format, ordered by priority:

```
## Tokenomics review — Optimization

| # | Finding | Location | Strategy | Est. impact | Quality risk |
|---|---------|----------|----------|-------------|--------------|
| 1 | Timestamp injected at top of system prompt breaks prefix cache | agents/orch.py:42 | Prompt-cache placement | High | None |

### 1. <finding title>
**Evidence:** <what you saw>
**Fix:** <concrete change, with a short snippet>
**Verify:** <what to measure before/after>
```

Impact scale: **High** = applies to every model call (cache, system prompt),
**Medium** = grows with session length or fan-out (context, tool repeats, routing),
**Low** = narrow path. Don't invent dollar figures; if the user provides token
counts or prices, you may estimate.

4. End with "Not checked" for anything you couldn't see (e.g. runtime config held
   outside the repo).

## Quick facts (Amazon Bedrock)

- Cache **write** costs ~125% of the input price; cache **read** ~5–10%. A prefix
  that is rewritten every call costs *more* than no cache.
- Bedrock caches by **prefix**: any byte change before a cache point invalidates
  everything after it.
- Up to **4** cache checkpoints per request; each needs a model-specific minimum
  token count. Default TTL 5 minutes (some models support 1 hour).
- With caching on, `inputTokens` excludes cached tokens. Total input =
  `inputTokens + cacheReadInputTokens + cacheWriteInputTokens`.
- Verify current prices, minimums and model support in the Bedrock docs before
  quoting numbers to a customer.

## Learn more

Reference solution with working, benchmarked implementations of these strategies:
[github.com/awslabs/agent-tokenomics-framework](https://github.com/awslabs/agent-tokenomics-framework)
(see the [README](https://github.com/awslabs/agent-tokenomics-framework/blob/main/README.md)).
