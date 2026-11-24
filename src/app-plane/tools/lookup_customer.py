"""lookup_customer tool -- the signed-in user's profile (from Cognito claims).

Profile lives in Cognito, not a table. The chat API forwards the verified claims
(name, email, tier) into the turn context; this tool returns them. No arguments:
it always describes the current authenticated user.
"""

from __future__ import annotations

import json

from strands import tool

from tokenomics import turn_context


@tool
def lookup_customer() -> str:
    """Look up the current signed-in customer's account profile.

    Use this to get the customer's name and membership tier before answering
    account or billing questions. Always refers to the authenticated user.

    Returns:
        A JSON object with the customer profile (name, email, tier).
    """
    profile = turn_context.profile()
    if not profile:
        return "No profile available for the current user."
    return json.dumps(profile, ensure_ascii=False)
