# 3. Context management

**Pillar:** Optimization (configure native) · **Quality risk:** Low–medium — older detail can be lost; keep recent turns intact.

## Problem

With memory/session restore, every prior turn — including large tool results — is
re-sent on every model call. Input cost grows each turn, and a long tool loop within a
single turn grows it further.

## Three layers (apply in order)

| Layer | What | When |
|---|---|---|
| Observation masking | Replace old `toolResult` (and optionally `toolUse`) blocks with a short placeholder; keep user questions and final replies | Always — tool results are usually most of the tokens |
| Sliding window | Keep the last N messages | Prompt caching **off** |
| Summarize | Fold the oldest messages into one summary once the prompt passes a token threshold | Prompt caching **on** |

### Why the choice depends on prompt caching

Caching matches from the **start** of the prompt. Trimming old messages changes the
start, so the next call *writes* the cache (~125%) instead of reading it (~10%).

- Caching **off** → trim often (sliding window); every removed token saves full price.
- Caching **on** → summarize rarely, in one big step; then enjoy cheap reads until
  the next threshold.

## Strands (configure, don't rebuild)

```python
from strands.agent.conversation_manager import (
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)

# Caching OFF: trim before every model call (per_turn=True also bounds long tool loops)
cm = SlidingWindowConversationManager(window_size=12, per_turn=True)

# Caching ON: summarize once the whole prompt crosses a threshold
cm = SummarizingConversationManager(
    summary_ratio=0.6,               # share of oldest messages folded
    preserve_recent_messages=4,      # never summarize the latest turns
    summarization_agent=cheap_agent, # small model, NO prompt cache (input differs each call)
    proactive_compression={"compression_threshold": 20_000 / model_context_window},
)

agent = Agent(model=model, conversation_manager=cm, ...)
```

## Pitfalls

- **Threshold too low:** if fixed prefix + summary + preserved messages is still above
  the threshold, the manager re-summarizes every call — summaries of summaries, facts
  fade, cache breaks every call. Keep the threshold well above the fixed prompt size.
- **Masking must preserve message count/alternation** when restoring from session
  storage; dropping messages can shift restore offsets or produce two assistant
  messages in a row (Converse rejects this). Replace content in place instead.
- **Masking output must be deterministic** so the masked prefix stays byte-stable (cache-friendly).
- **Attach the same conversation-manager class every turn** when using persisted
  sessions; switching classes mid-session can fail state restore.
- **Meter the summarizer.** Its usage is often discarded by the SDK — record it so savings
  are net, not gross.
- Specialists invoked single-shot per delegation usually don't need a manager.

## Code smells to look for

- `session_manager` / memory enabled with no `conversation_manager` configured.
- Tool results returning whole documents / full API payloads into history.
- `window_size` trimming every turn while `cache_config` is enabled.
- Summarizer built with the same expensive model as the main agent.

## Verify

- Plot average input tokens per **turn index** (turn 1, 2, 3, …) before/after; the
  optimized curve should flatten.
- With caching on, also check cache **write** cost didn't rise.
- Spot-check multi-turn answers that depend on early-turn facts.
