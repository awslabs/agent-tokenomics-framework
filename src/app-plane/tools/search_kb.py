"""search_kb tool -- canned FAQ knowledge lookup.

V1 stand-in for a Bedrock Knowledge Base: keyword match over faq.json. The
tool contract (name, args, docstring) is intentionally KB-shaped so the
implementation can be swapped for a real KB later without touching the agents.
"""

from __future__ import annotations

import store
from strands import tool


@tool
def search_kb(query: str) -> str:
    """Search the support knowledge base for policy and how-to answers.

    Use this for questions about returns, refunds, shipping times, cancellations,
    subscriptions, and payments -- anything that is general policy rather than a
    specific customer's account or orders.

    Args:
        query: The customer's question or the topic to look up.

    Returns:
        The most relevant knowledge-base answer(s), or a note that nothing matched.
    """
    hits = store.search_faq(query)
    if not hits:
        return "No knowledge-base article matched. Suggest contacting support for this topic."
    return "\n\n".join(f"[{h['id']}] {h['question']}\n{h['answer']}" for h in hits)
