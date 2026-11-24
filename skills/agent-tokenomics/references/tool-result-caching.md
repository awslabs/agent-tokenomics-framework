# 4. Intra-session tool result caching

**Pillar:** Optimization · **Quality risk:** Low — risk is stale data; restrict to read-only tools and short TTLs.

## Problem

Agents re-call the same tool with identical arguments within a session (re-checking an
order, re-searching the KB with the same query, sub-agents repeating the parent's
lookup). Each repeat costs tool latency, downstream API cost, and — because the result
re-enters context — input tokens again.

## Do

1. **Cache only read-only, idempotent tools** (lookups, searches, gets). Never cache
   tools with side effects (create, update, send, pay).
2. **Key on session + tool name + canonicalized args** (sorted JSON), so `{"a":1,"b":2}`
   and `{"b":2,"a":1}` hit the same entry.
3. **Scope to the session** and set a short TTL for data that changes (order status,
   inventory). Scope per-tenant/per-user — never share across users.
4. **Invalidate on mutation:** when a write tool touches an entity, drop cached reads
   for it.
5. **Return a compact cached result**, or a short note ("same as earlier result for
   ORD-1001") if the full result is already in context.

## Sketch (SDK-agnostic, Python)

```python
import json, time
from functools import wraps

def session_cached(ttl_s: int = 300):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, session_cache: dict, **kwargs):
            key = (fn.__name__, json.dumps(kwargs, sort_keys=True, default=str))
            hit = session_cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl_s:
                return hit[1]
            result = fn(*args, **kwargs)
            session_cache[key] = (time.monotonic(), result)
            return result
        return wrapper
    return deco
```

On Strands, put the cache in the tool body (or a tool wrapper) and hold
`session_cache` in per-session state (e.g. `agent.state` or your session store) — not
a module-level global shared across users.

## Code smells to look for

- The same tool + args appearing multiple times in one session's trace.
- Orchestrator and specialist both calling the same lookup for one user question.
- A module-level `lru_cache` on a tool (crosses users/sessions — a data-leak risk, not
  just a staleness one).

## Verify

- Count tool calls per session and duplicate-call rate (same name + args) before/after.
- Input tokens per turn on tool-heavy turns.
