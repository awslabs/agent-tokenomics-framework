"""Business tools exposed to the agents.

- search_kb: canned FAQ lookup (stands in for a Bedrock Knowledge Base in V1).
- lookup_customer / get_orders: read the mock JSON store.

Each is a Strands @tool; its docstring + type hints become the tool schema the
model sees. Keep these docstrings stable -- they are part of the cacheable
prefix once prompt-cache is on.
"""

from .get_orders import get_orders
from .lookup_customer import lookup_customer
from .search_kb import search_kb

__all__ = ["get_orders", "lookup_customer", "search_kb"]
