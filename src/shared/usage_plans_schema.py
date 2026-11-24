"""Token-governance config schema (the control<->app contract).

This is the config contract for the token-governance feature. Like `flags_schema`,
it is written by the control plane and read by the app plane (the agent's in-loop
enforcement). Kept additive and tolerant: unknown fields are ignored and missing
fields fall back to "no cap", so a stale reader degrades to *not enforcing* rather
than crashing or over-enforcing.

V1 model: ONE global default config every user inherits. There are no per-user or
per-tier values in V1 (tiering is a deferred feature). `user_overrides` is kept in
the schema but DORMANT (not written by the V1 UI) so the future tier feature can
build on it without a rewrite.

Two logical record kinds, both persisted in the shared `Tokenomics-UsagePlans`
table (one item per record, keyed by `plan_id`):

  - Per-agent limits -> plan_id = "agent#<name>"  (orchestrator / knowledge / order)
      In-loop soft caps the agent enforces:
        max_output_tokens_per_model_call, max_tool_calls, max_iterations,
        max_tokens_per_agent_call.

  - Per-user quota   -> plan_id = "user#default"  (the one global default)
                        plan_id = "user#<sub>"    (dormant override; not used in V1)
      In-loop per-user money limits (QuotaHook, ADR-001):
        daily_max_tokens_per_user, monthly_max_tokens_per_user,
        max_tokens_per_minute_per_user (TPM).

`0` means "no cap" for that dimension. All numeric fields round-trip through the
store's float<->Decimal boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Canonical agent names (match tokenomics.cost / delegation attribution keys).
AGENTS = ("orchestrator", "knowledge", "order")

# plan_id prefixes.
AGENT_PREFIX = "agent#"
USER_PREFIX = "user#"
USER_DEFAULT_ID = "user#default"


def agent_plan_id(agent: str) -> str:
    return f"{AGENT_PREFIX}{agent}"


def user_plan_id(user_id: str) -> str:
    return f"{USER_PREFIX}{user_id}"


# ---- per-agent limits (in-loop, soft) --------------------------------------


@dataclass
class AgentLimits:
    """In-loop governance caps for one agent (enforced inside the agent loop).

    A field set to 0 means "no cap" for that dimension. Defaults are all-0 so a
    fresh/empty table = baseline (never stops), matching the prompt-cache/memory
    "off by default" convention. There is no separate `enabled` flag: a limit
    applies exactly when its value is > 0.
    """

    # Output cap per model call (BedrockModel max_tokens -> inferenceConfig.maxTokens).
    max_output_tokens_per_model_call: int = 0
    # Tool-call cap per turn (ToolCapHook).
    max_tool_calls: int = 0
    # Iteration cutoff per agent call (native Strands Limits.turns).
    max_iterations: int = 0
    # Cumulative input+output tokens per agent invocation (native Limits.total_tokens).
    max_tokens_per_agent_call: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AgentLimits:
        d = data or {}
        return cls(
            max_output_tokens_per_model_call=int(d.get("max_output_tokens_per_model_call", 0) or 0),
            max_tool_calls=int(d.get("max_tool_calls", 0) or 0),
            max_iterations=int(d.get("max_iterations", 0) or 0),
            max_tokens_per_agent_call=int(d.get("max_tokens_per_agent_call", 0) or 0),
        )

    # Convenience for the enforcement layer -----------------------------------

    def output_tokens_cap(self) -> int | None:
        return self.max_output_tokens_per_model_call or None

    def turns_limit(self) -> int | None:
        return self.max_iterations or None

    def total_tokens_limit(self) -> int | None:
        return self.max_tokens_per_agent_call or None

    def tool_calls_limit(self) -> int | None:
        return self.max_tool_calls or None

    def as_strands_limits(self) -> dict | None:
        """This agent's caps as a Strands `Limits` kwargs dict (None => no caps).

        Maps the native-Limits dimensions only: iteration cutoff -> `turns`,
        agent-call token cap -> `total_tokens`. (The per-model-call output cap is
        applied at model construction via BedrockModel(max_tokens=...), and the
        tool-call cap is a hook, so neither belongs here.) A dimension is included
        only when its value is > 0. Used identically by the orchestrator turn and
        the specialist delegation calls, so the projection lives on the data that
        owns the fields rather than being re-derived in each caller.
        """
        out: dict = {}
        if self.turns_limit():
            out["turns"] = self.turns_limit()
        if self.total_tokens_limit():
            out["total_tokens"] = self.total_tokens_limit()
        return out or None


# ---- per-user quota (in-loop, QuotaHook) -----------------------------------


@dataclass
class UserQuota:
    """In-loop per-user money limits (QuotaHook). 0 means "no cap".

    In V1 this is ONE global default applied per user (counted per user, single
    shared number). `email` is only a display label on a dormant override row.
    """

    daily_max_tokens_per_user: int = 0
    monthly_max_tokens_per_user: int = 0
    max_tokens_per_minute_per_user: int = 0  # TPM (in-loop rolling-minute window)
    email: str = ""  # display label only; a dormant override is keyed by Cognito sub

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> UserQuota:
        d = data or {}
        return cls(
            daily_max_tokens_per_user=int(d.get("daily_max_tokens_per_user", 0) or 0),
            monthly_max_tokens_per_user=int(d.get("monthly_max_tokens_per_user", 0) or 0),
            max_tokens_per_minute_per_user=int(d.get("max_tokens_per_minute_per_user", 0) or 0),
            email=str(d.get("email", "") or ""),
        )


# ---- full config (all agent limits + the session cap + the user quota) -----


@dataclass
class GovernanceConfig:
    """The full governance config both planes agree on (V1: one global default).

    agents:         per-agent limits keyed by agent name.
    session_max_tokens: global per-session cumulative token cap (soft, in-loop).
    user_default:   the single global per-user quota (applies to every user).
    user_overrides: DORMANT per-user exceptions keyed by sub (not used in V1 UI;
                    seed for the future tier feature).
    """

    agents: dict[str, AgentLimits] = field(default_factory=dict)
    session_max_tokens: int = 0
    user_default: UserQuota = field(default_factory=UserQuota)
    user_overrides: dict[str, UserQuota] = field(default_factory=dict)

    # ---- (de)serialization from the flat table item list -------------------

    @classmethod
    def from_items(cls, items: list[dict[str, Any]]) -> GovernanceConfig:
        """Reassemble one-item-per-plan_id rows into the structured config.

        `session_max_tokens` is a global value; it is stored on the
        `user#default` row's config alongside the quota (single global row) to
        avoid a separate record kind. Reading tolerates it being absent.
        """
        agents: dict[str, AgentLimits] = {}
        session_max_tokens = 0
        user_default = UserQuota()
        user_overrides: dict[str, UserQuota] = {}

        for item in items or []:
            plan_id = str(item.get("plan_id", ""))
            body = item.get("config", {}) or {}
            if plan_id.startswith(AGENT_PREFIX):
                agents[plan_id[len(AGENT_PREFIX) :]] = AgentLimits.from_dict(body)
            elif plan_id == USER_DEFAULT_ID:
                user_default = UserQuota.from_dict(body)
                session_max_tokens = int(body.get("session_max_tokens", 0) or 0)
            elif plan_id.startswith(USER_PREFIX):
                user_overrides[plan_id[len(USER_PREFIX) :]] = UserQuota.from_dict(body)

        return cls(
            agents=agents,
            session_max_tokens=session_max_tokens,
            user_default=user_default,
            user_overrides=user_overrides,
        )

    def to_items(self) -> list[dict[str, Any]]:
        """Flatten to one item per plan_id for the table.

        The global `session_max_tokens` is co-located on the `user#default` row
        so there is exactly one global row (quota + session cap together).
        """
        items: list[dict[str, Any]] = []
        for name, limits in self.agents.items():
            items.append({"plan_id": agent_plan_id(name), "config": limits.to_dict()})
        default_cfg = self.user_default.to_dict()
        default_cfg["session_max_tokens"] = int(self.session_max_tokens or 0)
        items.append({"plan_id": USER_DEFAULT_ID, "config": default_cfg})
        for uid, q in self.user_overrides.items():
            items.append({"plan_id": user_plan_id(uid), "config": q.to_dict()})
        return items

    def to_dict(self) -> dict[str, Any]:
        """Nested view for the API/UI."""
        return {
            "agents": {name: a.to_dict() for name, a in self.agents.items()},
            "session_max_tokens": int(self.session_max_tokens or 0),
            "user_default": self.user_default.to_dict(),
            "user_overrides": {uid: q.to_dict() for uid, q in self.user_overrides.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GovernanceConfig:
        d = data or {}
        agents = {name: AgentLimits.from_dict(body) for name, body in (d.get("agents", {}) or {}).items()}
        overrides = {
            uid: UserQuota.from_dict(body) for uid, body in (d.get("user_overrides", {}) or {}).items()
        }
        return cls(
            agents=agents,
            session_max_tokens=int(d.get("session_max_tokens", 0) or 0),
            user_default=UserQuota.from_dict(d.get("user_default")),
            user_overrides=overrides,
        )

    @classmethod
    def empty(cls) -> GovernanceConfig:
        """No caps anywhere: agent never stops, no user quota. Safe fallback."""
        return cls()

    # ---- accessors the enforcement layers use ------------------------------

    def agent_limits(self, agent: str) -> AgentLimits:
        """The limits for an agent, or an all-off default (no cap)."""
        return self.agents.get(agent, AgentLimits())

    def quota_for(self, user_id: str) -> UserQuota:
        """The effective quota for a user: dormant override if present, else default.

        In V1 no overrides are written, so this returns the global default for
        everyone. Kept override-aware for the future tier feature.
        """
        return self.user_overrides.get(user_id, self.user_default)


def default_governance_config() -> dict[str, Any]:
    """Seed/fallback value: all agents present with 0 caps, no user quota."""
    return GovernanceConfig(
        agents={name: AgentLimits() for name in AGENTS},
        session_max_tokens=0,
        user_default=UserQuota(),
        user_overrides={},
    ).to_dict()
