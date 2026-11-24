"""Tokenomics hooks package.

V1 applies its patterns via the flag-aware factory (construction-time prompt
caching) and imperative cost capture in the orchestrator runner, not via
Strands HookProviders. This package is kept as the extension point for future
hook-based patterns (e.g. per-invocation budget enforcement).
"""
