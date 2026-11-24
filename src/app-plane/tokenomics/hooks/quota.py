"""QuotaHook -- in-loop per-user daily/monthly quota + TPM throttle (ADR-001).

This is the V1 replacement for the (removed) AgentCore Gateway interceptors. It
enforces the per-user *money* limits inside the agent loop, before the model is
called, via Strands' `BeforeModelCallEvent.cancel`. One shared instance is
attached to **every** agent (orchestrator + specialists) through the factory
chokepoint, so no model call -- on any agent, now or future -- can escape it.

Design (see design.md §4 + ADR-001 integrity rules):

  * **Pre-call gate.** On the first model call of a turn it reads the user's
    `EnforcementCounter` rows (daily, monthly, rolling-minute) and compares them
    to the effective `UserQuota`. Over the daily/monthly cap -> `reason=quota`;
    over the per-minute cap -> `reason=throttle`. Either way it sets
    `event.cancel=<message>` so the model call (and the turn) stops cleanly, and
    records the outcome on the shared `GovernanceOutcome`.
  * **Decide once per turn.** The counter read happens on the first model call
    and the decision is cached, so subsequent model calls in the same turn (and
    specialist calls) are near-zero cost.
  * **Fail-CLOSED.** If the counter cannot be read, the hook BLOCKS (it is a
    money limit; never let an unmetered turn through on a store hiccup).
  * **No caps / empty config => never fires.** 0 means "no cap" per dimension;
    an empty config (what benchmark runs pin) has an all-zero quota, so the hook
    is a no-op -- benchmarks are never throttled by live quotas.

The post-turn increment of the actual tokens spent (incl. cacheRead/cacheWrite)
lives in the orchestrator runner, not here, so it runs exactly once per turn with
the final usage totals.
"""

from __future__ import annotations

import logging

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeModelCallEvent

from shared.usage_plans_schema import UserQuota
from tokenomics import quota_counter
from tokenomics.governance_outcome import GovernanceOutcome

logger = logging.getLogger(__name__)


class QuotaHook(HookProvider):
    """Cancel a turn before its first model call when the user is over quota/TPM.

    Args:
        user_id: the Cognito sub the counters are keyed by.
        quota: the effective `UserQuota` for this user (0 => no cap per field).
        outcome: shared per-turn outcome to flag when a limit trips.
    """

    def __init__(self, user_id: str, quota: UserQuota, outcome: GovernanceOutcome):
        self.user_id = user_id or ""
        self.quota = quota or UserQuota()
        self.outcome = outcome
        # Memoized decision for this turn: None = not yet checked; (cancel, msg).
        self._decided: tuple[bool, str] | None = None

    # Any quota field > 0 means this user has at least one money limit to enforce.
    def _has_caps(self) -> bool:
        q = self.quota
        return bool(
            q.daily_max_tokens_per_user or q.monthly_max_tokens_per_user or q.max_tokens_per_minute_per_user
        )

    def register_hooks(self, registry: HookRegistry, **_: object) -> None:
        registry.add_callback(BeforeModelCallEvent, self._check)

    def _check(self, event: BeforeModelCallEvent) -> None:
        # No user / no caps / empty config (benchmark) => never fires.
        if not self.user_id or not self._has_caps():
            return

        # Decide once per turn; reuse for later model/specialist calls. Pass the
        # loop's projected input tokens so a single turn that would cross a cap is
        # blocked up front (critical for TPM: a per-minute cap smaller than one
        # turn would otherwise never fire on the first turn of a minute).
        if self._decided is None:
            projected = int(getattr(event, "projected_input_tokens", 0) or 0)
            self._decided = self._evaluate(projected)

        cancel, message = self._decided
        if cancel:
            event.cancel = message

    def _evaluate(self, projected: int = 0) -> tuple[bool, str]:
        """Read the counters and decide block/allow. Fail-CLOSED on read error.

        `projected` is the upcoming turn's estimated input tokens; comparing
        (already-spent + projected) to the cap blocks the turn that would cross
        it, rather than only the next one.
        """
        q = self.quota
        try:
            # Throttle first (shortest window, cheapest to breach), then quota.
            if q.max_tokens_per_minute_per_user:
                spent_min = quota_counter.read_minute(self.user_id)
                if spent_min + projected >= q.max_tokens_per_minute_per_user:
                    self.outcome.record(
                        stopped=True,
                        limited=False,
                        reason="throttle",
                        scope="user",
                        detail=(
                            f"per-minute token throttle reached ({q.max_tokens_per_minute_per_user:,}/min)"
                        ),
                        config="max_tokens_per_minute_per_user",
                    )
                    return True, ("You're sending requests too quickly. Please wait a moment and try again.")

            if q.daily_max_tokens_per_user:
                spent_day = quota_counter.read_day(self.user_id)
                if spent_day + projected >= q.daily_max_tokens_per_user:
                    self.outcome.record(
                        stopped=True,
                        limited=False,
                        reason="quota",
                        scope="user",
                        detail=f"daily token quota reached ({q.daily_max_tokens_per_user:,})",
                        config="daily_max_tokens_per_user",
                    )
                    return True, "You've reached today's usage limit. Please try again tomorrow."

            if q.monthly_max_tokens_per_user:
                spent_month = quota_counter.read_month(self.user_id)
                if spent_month + projected >= q.monthly_max_tokens_per_user:
                    self.outcome.record(
                        stopped=True,
                        limited=False,
                        reason="quota",
                        scope="user",
                        detail=f"monthly token quota reached ({q.monthly_max_tokens_per_user:,})",
                        config="monthly_max_tokens_per_user",
                    )
                    return True, "You've reached this month's usage limit."

        except quota_counter.CounterUnavailable as exc:
            # Fail-CLOSED: a money limit must not be bypassed by a store hiccup.
            logger.warning("[quota-hook] counter unavailable, failing closed (%s)", exc)
            self.outcome.record(
                stopped=True,
                limited=False,
                reason="quota",
                scope="user",
                detail="usage counter unavailable (fail-closed)",
                config="daily_max_tokens_per_user",
            )
            return True, "Usage limits can't be verified right now. Please try again shortly."

        return False, ""
