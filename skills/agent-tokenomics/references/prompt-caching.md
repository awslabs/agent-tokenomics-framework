# 1. Prompt-cache placement

**Pillar:** Optimization · **Quality risk:** None — requests are byte-identical, only billing changes.

## Problem

Every agent turn re-sends the same system prompt and tool specs. Multi-agent systems
multiply this: each delegation re-sends a specialist's prefix too. Uncached, you pay
full input price for identical tokens on every model call.

## How Bedrock prompt caching works

- Cache is keyed by **prefix**. Content up to a `cachePoint` is cached; a later call
  with the *exact same bytes* up to that point reads from cache.
- Any change before a cache point (timestamp, user name, request id, reordered tools)
  invalidates that point and everything after it.
- Up to **4** cache points per request. Each point needs a model-specific minimum
  number of tokens before it, or it's ignored.
- Write ≈ 125% of input price; read ≈ 5–10%. Break-even is roughly **one read per write**.
- TTL is 5 minutes by default (refreshed on each hit); some models support 1 hour.

## Do

1. **Order the prompt stable → volatile:** tools → system prompt → static few-shot /
   reference docs → conversation history → latest user message.
2. **Move dynamic values out of the prefix.** Dates, user names, tenant ids, session
   state belong in the user message (or a late system block after the last cache point),
   not at the top of the system prompt.
3. **Keep tool definitions deterministic** — same order and same text every call (no
   set/dict iteration order surprises, no per-request tool lists unless necessary).
4. **On Strands, configure natively** — don't hand-place cache blocks:

```python
from strands import Agent
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig

model = BedrockModel(
    model_id="global.anthropic.claude-sonnet-5-5",
    cache_config=CacheConfig(strategy="auto", ttl="5m"),  # SDK places cachePoints after tools -> system
)
agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=TOOLS)
```

5. **Raw Converse API** — add a `cachePoint` block after the stable content:

```python
system=[{"text": SYSTEM_PROMPT}, {"cachePoint": {"type": "default"}}]
```

## Don't

- Don't cache content that changes every call (e.g. a summarizer's input) — you pay
  the 125% write and never read it back.
- Don't combine caching with a context strategy that trims the *start* of history on
  every turn — each trim rewrites the cache. See [context-management.md](context-management.md).
- Don't put the cache point before the content has reached the model's minimum token count.

## Code smells to look for

- f-strings / `.format()` / `datetime.now()` inside the system prompt builder.
- `BedrockModel(...)` without `cache_config` on an agent with a large, stable prefix.
- Tool list built from a `set` or filtered per request.
- `cacheReadInputTokens` always 0 in logs while caching is "on".

## Verify

Compare across 2+ calls within the TTL:
- `cacheReadInputTokens` should climb on calls 2..N; `cacheWriteInputTokens` should be
  non-zero only on the first call (or after a deliberate prefix change).
- Cache hit ratio = `cacheRead / (input + cacheRead + cacheWrite)`.
- A hit ratio that drops after a deploy usually means someone put dynamic content in
  the prefix.
