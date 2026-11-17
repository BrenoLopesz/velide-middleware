"""Correlate full item snapshots without adding percentages across polling cycles."""
from connectors.farmax.identity import normalize_farmax_id


def correlate_progress(internal, expected, previous, progress, adopted=False):
    previous = previous or {}
    result = dict(progress, accepted=False)
    contexts = [item.get("source_context") for item in progress.get("items", [])]
    if not progress.get("snapshot_complete") or not contexts or not contexts[0] or any(value != contexts[0] for value in contexts):
        return dict(result, reason="A leitura dos itens está incompleta. Uma nova consulta será feita.")
    context = contexts[0]
    if normalize_farmax_id(context.get("sale_id", internal)) != internal:
        return dict(result, reason="A identificação da venda mudou. Confira o pedido no Farmax.")
    original = previous.get("source_context") or expected
    if not original and not adopted:
        return dict(result, reason="Não foi possível relacionar a venda ao pedido original. Confira o pedido no Farmax.")
    if original:
        for field in ("branch_id", "sale_id", "customer_id", "sale_date", "delivery_date", "address", "sale_time"):
            # Deletion removes the delivery date, not the retained sale identity.
            if field == "delivery_date" and context.get(field) is None:
                continue
            if field in original and original[field] != context.get(field):
                return dict(result, reason="A identificação ou os dados originais da venda mudaram. Confira o pedido no Farmax.")
    ledger = {tuple(item["key"]): item["status"] for item in previous.get("ledger", [])}
    if not ledger and original:
        for item in original.get("item_keys", []):
            ledger[(context["branch_id"], internal) + tuple(item)] = "UNKNOWN"
    current = {}
    for item in progress["items"]:
        key = tuple(normalize_farmax_id(item[field]) for field in ("branch_id", "id", "item_id", "item_order"))
        if key[:2] != (context["branch_id"], internal):
            return dict(result, reason="Os itens pertencem a outra venda ou filial. Confira o pedido no Farmax.")
        current[key] = (item.get("status") or "UNKNOWN").strip().upper()
    # A disappeared cancelled row remains evidence. Disappearance of an active
    # or never-observed row is NOT evidence of cancellation.
    for key in ledger:
        if key not in current and ledger[key] != "C":
            ledger[key] = "UNKNOWN"
    ledger.update(current)
    cancelled = sum(status == "C" for status in ledger.values())
    unknown = sum(status not in ("S", "V", "C", "D", "I") for status in ledger.values())
    result.update(accepted=True, source_context=context,
                  ledger=[{"key": list(key), "status": ledger[key]} for key in sorted(ledger)],
                  total_items=len(ledger), cancelled_items=cancelled, unknown_items=unknown,
                  percentage=100.0 * cancelled / len(ledger) if ledger else None)
    if unknown:
        result.update(decision="UNRESOLVED", reason="Alguns itens ainda não foram confirmados. Uma nova consulta será feita.")
    elif ledger and cancelled == len(ledger):
        result.update(decision="CANCEL", reason="Todos os itens estão cancelados no Farmax.")
    return result
