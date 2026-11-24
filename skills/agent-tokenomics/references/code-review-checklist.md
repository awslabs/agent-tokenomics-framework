# Optimization code-review checklist

Walk in order. Report only what you observed, with `file:line`. Mark anything you
couldn't determine from the code under "Not checked".

## 1. Prompt-cache placement — impact High
- [ ] Is caching enabled on models with a large stable prefix? (Strands: `BedrockModel(cache_config=CacheConfig(...))`; Converse: `cachePoint` blocks)
- [ ] Is anything dynamic (dates, `datetime.now()`, user/tenant name, ids, session state) placed **before** the cache point / at the top of the system prompt?
- [ ] Are tool lists deterministic in content and order on every call?
- [ ] Is caching enabled on calls whose input differs every time (summarizers, one-shot classifiers)? → wasted writes.

## 2. System prompt — impact High
- [ ] Prompt size reasonable for the agent's role? Specialists with orchestrator-sized prompts?
- [ ] Duplicate text across agents' prompts or between prompt and tool docstrings?
- [ ] Large static documents / schemas pasted inline that could be retrieved on demand?
- [ ] Internal (user-invisible) steps asked for prose instead of concise/structured output?

## 3. Context management — impact Medium (grows with session length)
- [ ] Session memory / history restore enabled without a conversation manager?
- [ ] Large tool results kept verbatim in history across turns (no observation masking)?
- [ ] Trimming strategy compatible with caching? (sliding window every turn + cache on = cache rewrite every turn)
- [ ] Summarizer on a cheap model, without prompt cache, and its usage metered?
- [ ] Summarization threshold comfortably above the fixed prompt size?

## 4. Tool result caching — impact Medium
- [ ] Same read-only tool likely called repeatedly with the same args in a session (parent + sub-agent, retries, re-checks)?
- [ ] Any existing cache shared across users/sessions (module-level `lru_cache`, global dict)? → flag as a **security** issue too.
- [ ] Write tools invalidate related cached reads?

## 5. Model routing — impact Medium
- [ ] All agents on the frontier model regardless of task difficulty?
- [ ] If routing exists: fail direction sensible per role; classifier cheaper than the tier gap; judge metered; cost attributed to the model that actually ran?

## 6. Request batching — impact Low–High (depends on volume)
- [ ] Bulk/offline loops calling on-demand `converse` / `invoke_model`?
- [ ] Scheduled jobs that could use `CreateModelInvocationJob`?

## Cross-cutting (mention if missing — needed to prove any saving)
- [ ] Are `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens` captured per call / per agent?
- [ ] Is cost priced per model (not tokens summed across models before pricing)?
