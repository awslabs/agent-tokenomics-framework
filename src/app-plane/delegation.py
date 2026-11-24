"""Delegation seam: orchestrator -> specialists, in-process (agents-as-tools).

V1 delegates in-process for ship speed and clean end-to-end tokenomics. The seam is kept narrow -- the orchestrator only ever calls `ask_knowledge` / `ask_order` -- so it can be promoted to A2A later (deploy each specialist as its own runtime, turn these into remote calls) without touching the orchestrator prompt or logic.

Each delegate call also records the specialist's token usage into the shared
UsageCollector under the specialist model's price, so cost attribution spans the
whole multi-agent run.
"""

from __future__ import annotations

from strands import Agent, tool

from tokenomics.cost import UsageCollector
from tokenomics.governance_outcome import GovernanceOutcome


def _priced_model(tracker, default_id: str) -> str:
    """The model to price a specialist's usage at: the one routing actually ran
    (tracker.last_model) when model_routing is ON, else the static default id."""
    if tracker is not None and getattr(tracker, "last_model", ""):
        return tracker.last_model
    return default_id


def make_specialist_tools(
    knowledge: Agent,
    order: Agent,
    knowledge_model_id: str,
    order_model_id: str,
    collector: UsageCollector,
    plans=None,
    outcome: GovernanceOutcome | None = None,
    knowledge_tracker=None,
    order_tracker=None,
):
    """Return [ask_knowledge, ask_order] tools bound to this invocation's agents.

    The tools are closures so the orchestrator can call the exact specialist
    instances built for this turn (with the right flag config) and so each
    delegated call's usage is attributed to the current UsageCollector.

    Per-agent governance (F2.1): each specialist is invoked with its own native
    Strands `Limits` (iteration + token caps from its plan). A cutoff surfaces via
    the specialist AgentResult.stop_reason, recorded on the shared outcome.
    """
    from shared.usage_plans_schema import GovernanceConfig

    plans = plans or GovernanceConfig.empty()
    know_limits = plans.agent_limits("knowledge").as_strands_limits()
    order_limits = plans.agent_limits("order").as_strands_limits()

    # Which config name a stop_reason maps to (for the LimitViolation event).
    _CONFIG_FOR = {
        "limit_turns": "max_iterations",
        "limit_total_tokens": "max_tokens_per_agent_call",
        "limit_output_tokens": "max_output_tokens_per_model_call",
    }

    def _note(result, agent_name: str) -> None:
        if outcome is not None:
            sr = getattr(result, "stop_reason", None)
            if sr in _CONFIG_FOR:
                outcome.record(
                    stopped=True,
                    limited=False,
                    reason=sr,
                    scope="agent",
                    detail=f"{agent_name} specialist hit {sr}",
                    config=_CONFIG_FOR[sr],
                    agent=agent_name,
                )

    @tool
    def ask_knowledge(question: str) -> str:
        """Ask the Knowledge specialist a general policy or how-to question.

        Use for return policy, refund timelines, shipping durations, cancellation
        rules, subscriptions, and payments -- anything not specific to one
        customer's orders.

        Args:
            question: The policy or how-to question, in plain English.

        Returns:
            The specialist's grounded answer.
        """
        result = knowledge(question, **({"limits": know_limits} if know_limits else {}))
        collector.record_result(
            _priced_model(knowledge_tracker, knowledge_model_id), result, agent="knowledge"
        )
        _note(result, "knowledge")
        return str(result)

    @tool
    def ask_order(task: str) -> str:
        """Ask the Order specialist about this customer's account or orders.

        Use for order status, tracking, delivery ETA, order history, subscription
        status, or refund/return eligibility on an order they placed. The
        specialist always acts on the currently signed-in user.

        Args:
            task: What to look up, in plain English.

        Returns:
            The specialist's grounded answer.
        """
        result = order(task, **({"limits": order_limits} if order_limits else {}))
        collector.record_result(_priced_model(order_tracker, order_model_id), result, agent="order")
        _note(result, "order")
        return str(result)

    return [ask_knowledge, ask_order]
