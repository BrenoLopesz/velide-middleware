"""Delivery-level interpretation of a complete Farmax sale snapshot.

These rules do not infer refunds, fiscal cancellation, or courier returns.
"""
from enum import Enum
from typing import Sequence

from models.farmax_models import FarmaxSale


class CancellationDecision(str, Enum):
    ACTIVE = "ACTIVE"
    CANCEL = "CANCEL"
    UNRESOLVED = "UNRESOLVED"


def classify_cancellation(items: Sequence[FarmaxSale]) -> CancellationDecision:
    if not items:
        return CancellationDecision.UNRESOLVED
    statuses = {item.status.strip().upper() for item in items}
    # Whole-sale C is distinct from an individual C/D item among active items.
    if statuses == {"C"}:
        return CancellationDecision.CANCEL
    removed = all(item.delivery_exists is False and item.delivery_deleted for item in items)
    concluded = all(item.concluded == "S" for item in items)
    # Pre-sale I/V branches and the checkout mixed-state cancellation need
    # independent logistics removal evidence. V by itself proves nothing.
    if removed and concluded and statuses <= {"I", "V", "C"}:
        return CancellationDecision.CANCEL
    if any(item.delivery_exists is False for item in items) or statuses.intersection({"C", "D", "I"}):
        return CancellationDecision.UNRESOLVED
    return CancellationDecision.ACTIVE
