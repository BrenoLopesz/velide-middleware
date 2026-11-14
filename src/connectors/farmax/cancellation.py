"""Interpret complete sale snapshots, never a product-code or status delta.

Percentages count item rows (not quantities or financial refunds). Re-reading
all rows naturally includes cancellations committed in earlier poll cycles.
"""
from enum import Enum
from typing import Sequence

from connectors.farmax.identity import normalize_farmax_id
from models.farmax_models import FarmaxSale


class CancellationDecision(str, Enum):
    ACTIVE = "ACTIVE"
    CANCEL = "CANCEL"
    UNRESOLVED = "UNRESOLVED"
    REMOVAL_CANDIDATE = "REMOVAL_CANDIDATE"


def cancellation_progress(items: Sequence[FarmaxSale]) -> dict:
    """JSON-safe evidence for logging and durable journal correlation.

    Persist/replace this snapshot by source incarnation; never add counts from
    successive polls. Missing rows require reconciliation with retained keys.
    """
    present = [item for item in items if item.item_present]
    statuses = [(item.status or "").strip().upper() for item in present]
    keys = []
    complete = bool(present) and len(present) == len(items)
    try:
        for item in present:
            keys.append(tuple(normalize_farmax_id(value) for value in
                              (item.branch_id, item.id, item.item_id, item.item_order)))
        complete = complete and len(set(keys)) == len(keys) and len({key[:2] for key in keys}) == 1
    except (ValueError, TypeError):
        complete = False
    complete = complete and all(item.evidence_complete and item.snapshot_item_count == len(present) for item in items)
    cancelled = statuses.count("C")
    unknown = sum(status not in {"S", "V", "C", "D", "I"} for status in statuses)
    decision = CancellationDecision.UNRESOLVED
    reason = "Evidência incompleta ou identidade de item ambígua."
    if complete and not unknown:
        removed = all(item.delivery_exists is False and item.delivery_deleted for item in items)
        if cancelled == len(present):
            decision = CancellationDecision.CANCEL
            reason = "Todos os itens estão cancelados no Farmax."
        elif removed:
            # Pré-vendas Del yields I or V; checkout's EOF anomaly can leave
            # S/C/V mixtures. Physical deletion plus a full concluded snapshot
            # establishes delivery removal, not a financial refund.
            if set(statuses) <= {"S", "V", "C", "I"} and all(item.concluded == "S" for item in items):
                decision = CancellationDecision.CANCEL
                reason = "Entrega removida no Farmax, confirmada pelo registro de exclusão e pela venda completa."
            else:
                decision = CancellationDecision.REMOVAL_CANDIDATE
                reason = "Entrega removida; estado da venda requer reconciliação."
        elif all(item.delivery_exists is True for item in items) and set(statuses) <= {"S", "V", "C", "D"} and set(statuses).intersection({"S", "V"}):
            decision = CancellationDecision.ACTIVE
            reason = "Cancelamento parcial; entrega permanece ativa." if cancelled else "Entrega permanece ativa."
        else:
            reason = "Estado da venda ou ausência da entrega requer reconciliação."
    return {
        "decision": decision.value,
        "reason": reason,
        "total_items": len(present),
        "cancelled_items": cancelled,
        "active_items": sum(status in {"S", "V"} for status in statuses),
        "returned_items": statuses.count("D"),
        "unknown_items": unknown,
        "percentage": 100.0 * cancelled / len(present) if complete else None,
        "snapshot_complete": bool(complete),
        "item_keys": [list(key) for key in sorted(keys)],
        "items": [item.model_dump(mode="json") for item in items],
    }


def classify_cancellation(items: Sequence[FarmaxSale]) -> CancellationDecision:
    return CancellationDecision(cancellation_progress(items)["decision"])
