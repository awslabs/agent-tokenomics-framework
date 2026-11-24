"""get_orders tool -- the signed-in user's orders (with shipping status).

Orders are keyed by the user's Cognito `sub`, taken from the turn context (set
from the verified JWT). No arguments: it always returns the current user's orders.
"""

from __future__ import annotations

import json

import store
from strands import tool

from tokenomics import turn_context


@tool
def get_orders() -> str:
    """Get the current customer's orders, including status and shipping tracking.

    Use this for questions about a specific order, delivery, tracking, or a
    refund/return on an order the customer already placed. Always refers to the
    authenticated user.

    Returns:
        A JSON array of the customer's orders, or a note if they have none.
    """
    uid = turn_context.user_id()
    if not uid:
        return "No authenticated user in context."
    orders = store.get_orders_for_user(uid)
    if not orders:
        return "You have no orders on file."
    return json.dumps(orders, ensure_ascii=False)
