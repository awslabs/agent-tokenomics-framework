"""Identity + run context stamped on every agent invocation.

Foundation for all attribution: the same identity (user, session, run, flag
config) is attached to the run so token cost can be rolled up per user / per
run / per flag config. Identity comes from the VERIFIED Cognito JWT (the chat
API passes `user_id` = Cognito `sub` and the user's profile), never from
client-supplied data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunContext:
    """Identity for one agent invocation (one customer turn or one benchmark convo)."""

    # Canonical user key = Cognito `sub` (opaque, verified). Orders are keyed by it.
    user_id: str
    # The signed-in user's profile (name, email, tier) forwarded from JWT claims.
    profile: dict[str, Any] = field(default_factory=dict)
    session_id: str = field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:12]}")
    transaction_id: str = field(default_factory=lambda: f"txn-{uuid.uuid4().hex[:12]}")
    # Present only during benchmark runs; None for live chat.
    run_id: str | None = None
    # The flag signature active for this invocation, e.g. "prompt_cache=on".
    flag_signature: str = "baseline"

    def as_metadata(self) -> dict[str, str]:
        """Flatten to string metadata (requestMetadata-shaped for the deployed path)."""
        md = {
            "user_id": self.user_id,
            "tier": str(self.profile.get("tier", "")),
            "session_id": self.session_id,
            "transaction_id": self.transaction_id,
            "flag_config": self.flag_signature,
        }
        if self.run_id:
            md["run_id"] = self.run_id
        return md
