"""Feature-flag schema shared by both planes.

V1 toggles: prompt-cache placement, short-term memory and context management.
All strategy code is
baked into the agent and defaults OFF; the control plane flips a flag and the
agent reads it (no redeploy). More flags are added here as features land.

Design note: keep this schema tiny and additive. The agent treats any unknown
flag as OFF, and the control plane treats a missing flag as its default, so the
two planes can be deployed independently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# The canonical flag identifiers. Add new features here as they ship.
PROMPT_CACHE = "prompt_cache"
MEMORY = "memory"
CONTEXT_MANAGEMENT = "context_management"
MODEL_ROUTING = "model_routing"

ALL_FEATURES = (PROMPT_CACHE, MEMORY, CONTEXT_MANAGEMENT, MODEL_ROUTING)

# context_management strategies. "auto" resolves from the prompt_cache flag.
CTX_AUTO = "auto"
CTX_SLIDING_WINDOW = "sliding_window"
CTX_SUMMARIZING = "summarizing"
CTX_STRATEGIES = (CTX_AUTO, CTX_SLIDING_WINDOW, CTX_SUMMARIZING)


@dataclass
class PromptCacheParams:
    """Tunables for the prompt-cache placement hook.

    Attributes:
        cache_system: place a cachePoint after the (stable) system prompt.
        cache_tools: place a cachePoint after the (stable) tool schemas.
        ttl: Bedrock cache TTL. "5m" (default) or "1h".
    """

    cache_system: bool = True
    cache_tools: bool = True
    ttl: str = "5m"


@dataclass
class MemoryParams:
    """Tunables for the short-term memory pattern.

    Attributes:
        batch_size: event-write batching. 1 = write each turn immediately.

    Note: short-term memory replays the conversation's stored turns; there is no
    replay-depth knob in V1 (a `retrieval_top_k` / retrieval_config applies to
    long-term strategy retrieval, which is V2). Keep this minimal so a param
    can't promise behavior the V1 path doesn't implement.
    """

    batch_size: int = 1


@dataclass
class ContextManagementParams:
    """Tunables for the context-management pattern (orchestrator history).

    Attributes:
        mask_tool_history: observation masking (primary compaction) -- on memory
            restore, replace prior turns' tool calls / specialist answers with
            short placeholders. Count-preserving, deterministic, cache-friendly.
        strategy: "auto" | "sliding_window" | "summarizing". "auto" = sliding
            window when prompt cache is OFF, summarizing when it is ON (trimming
            every turn would break the history cache every turn).
        window_size: sliding window -- messages kept.
        compress_at_tokens: summarizing -- summarize once the WHOLE prompt
            (system + tools + history) reaches this many tokens. High by default
            (safety net for long chats); must stay well above the ~1.5k-token
            fixed prefix or it re-summarizes on every call.
        summary_ratio: summarizing -- share of oldest messages folded (0.1-0.8).
        preserve_recent: summarizing -- most-recent messages never summarized.
        summarizer_model: model id for the summary; "" => the knowledge (Haiku) model.

    No `pin_first`: on memory restore Strands skips the first
    removed_message_count stored messages, which cuts pinned ones.
    """

    mask_tool_history: bool = True
    strategy: str = CTX_AUTO
    window_size: int = 12
    compress_at_tokens: int = 20000
    summary_ratio: float = 0.6
    preserve_recent: int = 4
    summarizer_model: str = ""

    def resolved_strategy(self, prompt_cache_on: bool) -> str:
        """The concrete strategy for this config ("auto" resolved)."""
        if self.strategy == CTX_AUTO:
            return CTX_SUMMARIZING if prompt_cache_on else CTX_SLIDING_WINDOW
        return self.strategy


@dataclass
class ModelRoutingParams:
    """Tunables for the model-routing pattern (on/off only in the UI).

    Model ids resolve from `config.py` when left blank, so the actual model ids
    live in one place (config) rather than being duplicated in the flag store.

    Attributes:
        capable_model: the capable/frontier tier; "" => config.ORCHESTRATOR_MODEL
            (Claude Sonnet 5.5). The orchestrator's default + the specialists'
            escalation target.
        cheap_model: the cheap tier; "" => config.KNOWLEDGE_MODEL (Claude Haiku
            5.5). The specialists' default + the orchestrator's route-down target.
        judge_model: the LLM-as-judge classifier for the orchestrator; ""
            => config.ROUTER_JUDGE_MODEL (Amazon Nova Lite). Judge only -- never
            an agent's runtime model.
    """

    capable_model: str = ""
    cheap_model: str = ""
    judge_model: str = ""


@dataclass
class FeatureFlag:
    """A single feature flag: enabled state + feature-specific params."""

    enabled: bool = False
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class FlagConfig:
    """The full flag configuration both planes agree on.

    Serialized to/from a plain dict for DynamoDB (deployed) or the JSON store
    (local). `flag_signature()` produces a stable, compact tag that is stamped
    on every model call so benchmark runs can be attributed to an exact config.
    """

    prompt_cache: FeatureFlag = field(default_factory=FeatureFlag)
    memory: FeatureFlag = field(default_factory=FeatureFlag)
    context_management: FeatureFlag = field(default_factory=FeatureFlag)
    model_routing: FeatureFlag = field(default_factory=FeatureFlag)

    # ---- (de)serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            PROMPT_CACHE: asdict(self.prompt_cache),
            MEMORY: asdict(self.memory),
            CONTEXT_MANAGEMENT: asdict(self.context_management),
            MODEL_ROUTING: asdict(self.model_routing),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FlagConfig:
        data = data or {}
        pc = data.get(PROMPT_CACHE, {}) or {}
        mem = data.get(MEMORY, {}) or {}
        ctx = data.get(CONTEXT_MANAGEMENT, {}) or {}
        mr = data.get(MODEL_ROUTING, {}) or {}
        return cls(
            prompt_cache=FeatureFlag(
                enabled=bool(pc.get("enabled", False)),
                params=dict(pc.get("params", {})),
            ),
            memory=FeatureFlag(
                enabled=bool(mem.get("enabled", False)),
                params=dict(mem.get("params", {})),
            ),
            context_management=FeatureFlag(
                enabled=bool(ctx.get("enabled", False)),
                params=dict(ctx.get("params", {})),
            ),
            model_routing=FeatureFlag(
                enabled=bool(mr.get("enabled", False)),
                params=dict(mr.get("params", {})),
            ),
        )

    @classmethod
    def all_off(cls) -> FlagConfig:
        """The baseline: every feature disabled (baseline agent behavior)."""
        return cls()

    # ---- typed accessors ---------------------------------------------------

    def prompt_cache_params(self) -> PromptCacheParams:
        p = self.prompt_cache.params or {}
        return PromptCacheParams(
            cache_system=bool(p.get("cache_system", True)),
            cache_tools=bool(p.get("cache_tools", True)),
            ttl=str(p.get("ttl", "5m")),
        )

    def memory_params(self) -> MemoryParams:
        p = self.memory.params or {}
        return MemoryParams(
            batch_size=int(p.get("batch_size", 1)),
        )

    def context_management_params(self) -> ContextManagementParams:
        p = self.context_management.params or {}
        strategy = str(p.get("strategy", CTX_AUTO))
        return ContextManagementParams(
            mask_tool_history=bool(p.get("mask_tool_history", True)),
            strategy=strategy if strategy in CTX_STRATEGIES else CTX_AUTO,
            window_size=max(2, int(p.get("window_size", 12))),
            compress_at_tokens=max(500, int(p.get("compress_at_tokens", 20000))),
            summary_ratio=max(0.1, min(0.8, float(p.get("summary_ratio", 0.6)))),
            preserve_recent=max(0, int(p.get("preserve_recent", 4))),
            summarizer_model=str(p.get("summarizer_model", "") or ""),
        )

    def model_routing_params(self) -> ModelRoutingParams:
        p = self.model_routing.params or {}
        return ModelRoutingParams(
            capable_model=str(p.get("capable_model", "") or ""),
            cheap_model=str(p.get("cheap_model", "") or ""),
            judge_model=str(p.get("judge_model", "") or ""),
        )

    # ---- benchmark identity ------------------------------------------------

    def flag_signature(self) -> str:
        """Compact human-readable tag, e.g. 'prompt_cache=on' / 'baseline'."""
        on = [name for name in ALL_FEATURES if getattr(self, name).enabled]
        return "+".join(f"{n}=on" for n in on) if on else "baseline"


def default_flag_config() -> dict[str, Any]:
    """The seed value written to the store on first run: all flags off."""
    return FlagConfig.all_off().to_dict()
