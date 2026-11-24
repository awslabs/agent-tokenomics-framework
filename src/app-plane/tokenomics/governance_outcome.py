"""GovernanceOutcome -- the enforcement result for one turn.

Threaded through `handle_turn`: the ToolCapHook, the native-Limits stop-reason
check, and the per-session pre-turn check all write to a single instance. It is
serialized onto the turn response (`TurnResult.governance`) so the chat UI can
show "response was shortened / limited", and included in the metering event.

Semantics:
  stopped  -> the turn was cut short before completing (hard-ish cutoff).
  limited  -> a cap acted but the turn still produced a usable answer
              (e.g. a tool call was blocked but the model answered anyway).
  reason   -> machine tag: limit_turns | limit_total_tokens | limit_output_tokens
              | tool_cap | session_budget | quota | throttle
  scope    -> turn | agent | session | user
  detail   -> human-readable one-liner for the event log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Strands stop_reason -> (reason tag, human detail, config name that fired)
_LIMIT_STOP_REASONS = {
    "limit_turns": ("limit_turns", "iteration cutoff reached", "max_iterations"),
    "limit_total_tokens": ("limit_total_tokens", "agent-call token cap reached", "max_tokens_per_agent_call"),
    "limit_output_tokens": (
        "limit_output_tokens",
        "output-token cap reached",
        "max_output_tokens_per_model_call",
    ),
    "max_tokens": (
        "limit_output_tokens",
        "model hit output cap for a step",
        "max_output_tokens_per_model_call",
    ),
}


@dataclass
class GovernanceOutcome:
    """Mutable per-turn enforcement outcome. Defaults = nothing acted."""

    stopped: bool = False
    limited: bool = False
    reason: str | None = None
    scope: str | None = None
    detail: str | None = None
    # The config name that fired (one of the eight), for the LimitViolation event.
    config: str | None = None
    # The agent involved (per-agent scope), if any.
    agent: str | None = None
    # Kept for the event log / metering; first trip wins for the headline fields.
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        stopped: bool = False,
        limited: bool = False,
        reason: str | None = None,
        scope: str | None = None,
        detail: str | None = None,
        config: str | None = None,
        agent: str | None = None,
    ) -> None:
        """Record one enforcement action. The first stop wins the headline."""
        self.events.append(
            {
                "stopped": stopped,
                "limited": limited,
                "reason": reason,
                "scope": scope,
                "detail": detail,
                "config": config,
                "agent": agent,
            }
        )
        if stopped:
            self.stopped = True
        if limited:
            self.limited = True
        # Headline reason/scope: prefer the first *stop*, else the first limit.
        if reason and (self.reason is None or (stopped and self.scope != "turn")):
            self.reason = reason
            self.scope = scope
            self.detail = detail
            self.config = config
            self.agent = agent

    def note_stop_reason(self, stop_reason: str | None) -> None:
        """Interpret a Strands AgentResult.stop_reason for native-Limits cutoffs."""
        if not stop_reason:
            return
        mapped = _LIMIT_STOP_REASONS.get(stop_reason)
        if mapped is None:
            return  # normal stop (end_turn / tool_use / etc.) -- not a cutoff
        reason, detail, cfg = mapped
        # max_tokens on a single step "limits" without necessarily stopping the turn;
        # the loop-level limit_* reasons mean the turn was cut short.
        stopped = stop_reason != "max_tokens"
        self.record(
            stopped=stopped, limited=not stopped, reason=reason, scope="turn", detail=detail, config=cfg
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "limited": self.limited,
            "reason": self.reason,
            "scope": self.scope,
            "detail": self.detail,
            "config": self.config,
            "agent": self.agent,
        }

    def acted(self) -> bool:
        """True if any cap acted this turn (for deciding whether to log an event)."""
        return self.stopped or self.limited
