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
    REMOVAL_CANDIDATE = "REMOVAL_CANDIDATE"


def classify_cancellation(items: Sequence[FarmaxSale]) -> CancellationDecision:
    if not items or any(not item.evidence_complete or not isinstance(item.status, str) for item in items):
        return CancellationDecision.UNRESOLVED
    statuses = {item.status.strip().upper() for item in items}
    removed = all(item.delivery_exists is False and item.delivery_deleted for item in items)
    # S/C mixtures also occur after checkout's partial-apply anomaly. Physical
    # removal is independent of financial status, but branchless logs cannot
    # prove an incarnation or the user's intent: review, never auto-cancel.
    if removed:
        return CancellationDecision.REMOVAL_CANDIDATE
    if any(item.delivery_exists is False for item in items) or statuses.intersection({"C", "D", "I"}):
        return CancellationDecision.UNRESOLVED
    return CancellationDecision.ACTIVE if statuses <= {"S", "V"} else CancellationDecision.UNRESOLVED
