"""ToolCapHook -- per-turn tool-call cap (token-governance, in-loop).

Strands native `Limits` caps turns and tokens but has no tool-count field, so we
add this thin HookProvider for feature F1.3 (tool-call cap). It counts tool calls
per invocation and cancels any call past the cap via `BeforeToolCallEvent.cancel_tool`,
which feeds an error-status tool result back to the model (so it stops retrying).

Deterministic (always fires) and enforced on real events, not model-asserted
labels -- so it is a sound soft cap. The counter resets each invocation
(`BeforeInvocationEvent`), so the cap is per-turn, not for the agent's lifetime.

When a cap trips it records the outcome on a shared `GovernanceOutcome` so the
runner can surface it to the UI and the metering event.
"""

from __future__ import annotations

from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from tokenomics.governance_outcome import GovernanceOutcome


class ToolCapHook(HookProvider):
    """Cancel tool calls beyond `max_calls` within a single turn.

    Args:
        max_calls: max tool invocations allowed per turn (0/None => no cap).
        outcome: shared outcome object to flag when the cap trips.
        agent_name: which agent this hook guards (for the outcome scope).
    """

    def __init__(self, max_calls: int, outcome: GovernanceOutcome, agent_name: str = "orchestrator"):
        self.max_calls = int(max_calls or 0)
        self.outcome = outcome
        self.agent_name = agent_name
        self._count = 0

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeInvocationEvent, self._reset)
        registry.add_callback(BeforeToolCallEvent, self._check)

    def _reset(self, _event: BeforeInvocationEvent) -> None:
        self._count = 0

    def _check(self, event: BeforeToolCallEvent) -> None:
        if self.max_calls <= 0:
            return
        self._count += 1
        if self._count > self.max_calls:
            name = event.tool_use.get("name", "tool")
            event.cancel_tool = (
                f"Tool-call budget reached ({self.max_calls} per turn). "
                f"Do not call '{name}' again; answer with what you have."
            )
            self.outcome.record(
                stopped=False,
                limited=True,
                reason="tool_cap",
                scope="agent" if self.agent_name != "orchestrator" else "turn",
                detail=f"{self.agent_name}: tool-call cap {self.max_calls} reached",
                config="max_tool_calls",
                agent=self.agent_name,
            )
