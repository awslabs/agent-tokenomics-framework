"""The float <-> Decimal boundary for DynamoDB, in one place for both planes.

DynamoDB has no float type: numbers are stored as `Decimal`. Application code
wants plain `int`/`float`. Every reader/writer crossing that boundary needs the
same two conversions, so they live here and both planes + the seed scripts import
them -- rather than each re-implementing the recursion (and drifting).

- `to_dynamo(obj)`  -- on WRITE: float -> Decimal (recursively through dict/list).
- `to_plain(obj)`   -- on READ:  Decimal -> int when integral, else float.

`to_plain` returns an int for an integral Decimal (e.g. `Decimal("2") -> 2`) and a
float otherwise (`Decimal("2.5") -> 2.5`). That matches how the handlers and the
agent expect their numbers typed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def to_dynamo(obj: Any) -> Any:
    """Recursively convert floats to Decimal for a DynamoDB write."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dynamo(v) for v in obj]
    return obj


def to_plain(obj: Any) -> Any:
    """Recursively convert DynamoDB Decimals to plain int/float for app code."""
    if isinstance(obj, Decimal):
        # Integral Decimals become int; everything else becomes float.
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    return obj
