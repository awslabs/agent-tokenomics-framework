# 2. System-prompt optimization

**Pillar:** Optimization · **Quality risk:** Low — remove filler, not instructions; re-check with an eval set.

## Problem

The system prompt is paid on **every model call** of every turn of every session
(cached or not). Bloat compounds: 1,000 wasted tokens × 5 calls/turn × 10 turns ×
10k sessions/day adds up fast. Disorder also breaks caching.

## Do

1. **Trim filler:** politeness, repeated restatements, "You are a helpful…" paragraphs,
   long lists of things the model already does by default.
2. **Deduplicate with tool descriptions.** If a tool's docstring already explains when
   to use it, don't repeat it in the system prompt.
3. **Prefer rules over examples.** Keep few-shot examples only where they measurably
   change behavior; keep them short.
4. **Order for caching:** stable instructions first, anything dynamic last (or move it
   to the user message). See [prompt-caching.md](prompt-caching.md).
5. **Specialists get specialist prompts.** Sub-agents should not inherit the
   orchestrator's full prompt.
6. **Ask for concise internal output** on steps the end user never sees (routing,
   tool-selection, verification). Prose there is pure output-token cost.

## Don't

- Don't remove safety, compliance, or output-format instructions to save tokens.
- Don't stuff retrieval content (whole docs, policies) into the system prompt — retrieve
  on demand instead.
- Don't build the system prompt per request with user-specific values at the top.

## Code smells to look for

- System prompt > ~1,500 tokens for a narrow specialist.
- Same paragraph present in multiple agents' prompts.
- Large static documents / JSON schemas pasted into the prompt.
- Prompt assembled with string concatenation of per-request data before static text.

## Verify

- Count prompt tokens before/after (Bedrock `CountTokens` API or the `inputTokens` of a
  first, uncached call).
- Run the same eval set before/after and compare answer quality, not just cost.
