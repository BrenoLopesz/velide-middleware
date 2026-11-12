from datetime import time
import pytest
from sqlalchemy import text, event
from connectors.farmax.cancellation import classify_cancellation, CancellationDecision


def effect(repo, operation="IN_ROUTE"):
    source = repo.fetch_deliveries_by_id((123,), pending_only=False)[0]
    return dict(source_context=source.source_context, operation=operation, courier_local_id="5",
                started_at="2026-11-11T15:00:00Z", completed_at="2026-11-11T16:00:00Z" if operation == "DONE" else None)


def test_composite_customer_and_string_product_identity(farmax_stack):
    repo, _ = farmax_stack
    delivery = repo.fetch_deliveries_by_id((123,), pending_only=False)[0]
    assert delivery.customer_contact == "111"
    assert delivery.source_context["branch_id"] == "1"
    assert delivery.source_context["item_keys"] == [["101", "1"], ["102", "2"]]


def test_reads_existing_route_without_any_source_writes(farmax_stack):
    repo, engine = farmax_stack
    expected = effect(repo)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, params, ctx, many: statements.append(sql))
    assert repo.observe_delivery_effect(123, expected, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_existing_completion_matches_without_replaying_departure(farmax_stack):
    repo, engine = farmax_stack
    expected = effect(repo, "DONE")
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V', HORA_CHEGADA='13:00:00'"))
        conn.execute(text("UPDATE VENDEDORES SET STATUS='I' WHERE CD_VENDEDOR=5"))
    assert repo.observe_delivery_effect(123, expected, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    expected["operation"] = "IN_ROUTE"
    assert repo.observe_delivery_effect(123, expected, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"


@pytest.mark.parametrize("sql", [
    "UPDATE VENDAS SET CONCLUIDO='N' WHERE ID_PRODUTO=101",
    "UPDATE VENDAS SET STATUS='D' WHERE ID_PRODUTO=101",
    "UPDATE VENDAS SET STATUS=NULL WHERE ID_PRODUTO=101",
    "UPDATE VENDAS SET CD_FILIAL=2 WHERE ID_PRODUTO=101",
    "INSERT INTO ENTREGAS(CD_VENDA,CD_FILIAL) VALUES(123,2)",
    "UPDATE VENDAS SET ID_PRODUTO=101,ORDEM=1 WHERE ID_PRODUTO=102",
    "UPDATE ENTREGAS SET CD_ENTREGADOR=6",
    "UPDATE ENTREGAS SET HORA_SAIDA='11:59:00'",
    "UPDATE ENTREGAS SET DATA='2026-11-12'",
])
def test_conflicting_source_never_gets_repaired(farmax_stack, sql):
    repo, engine = farmax_stack
    expected = effect(repo)
    with engine.begin() as conn:
        conn.execute(text(sql))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, params, ctx, many: statements.append(sql))
    with pytest.raises((ValueError, TypeError)):
        repo.observe_delivery_effect(123, expected, "America/Sao_Paulo")
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)


def test_no_automatic_cashier_or_completion_writer(farmax_stack):
    repo, engine = farmax_stack
    with pytest.raises(ValueError, match="disabled"):
        repo.update_delivery_as_done(123, time(13))
    with pytest.raises(ValueError, match="disabled"):
        repo.update_delivery_as_in_route(123,5,time(12),historical=True)
    expected = effect(repo, "DONE")
    with pytest.raises(ValueError, match="Farmax-side return"):
        repo.observe_delivery_effect(123, expected, "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R",None)
        assert conn.execute(text("SELECT CONCLUIDO,HORAFINAL,CHEQUE FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == ("S","11:05:00","retained")


def test_conflicting_item_address_does_not_pick_an_arbitrary_row(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET TEMPENDERECO='different address' WHERE ID_PRODUTO=102"))
    with pytest.raises(ValueError, match="Conflicting"):
        repo.fetch_deliveries_by_id((123,), pending_only=False)


def test_null_status_keeps_complete_group_and_does_not_poison_other_sale(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS=NULL WHERE ID_PRODUTO=101"))
        conn.execute(text("INSERT INTO VENDAS(CD_VENDA,CD_FILIAL,STATUS,CONCLUIDO,TIPO_VENDA) VALUES(124,1,'C','S','V')"))
        conn.execute(text("INSERT INTO DELIVERYLOG VALUES(1,124,'DELETE','2026-11-11 12:00:00')"))
    rows = repo.fetch_sales_statuses_by_id((123,124))
    assert len([row for row in rows if row.id == 123]) == 2
    assert classify_cancellation([row for row in rows if row.id == 123]) == CancellationDecision.UNRESOLVED
    assert classify_cancellation([row for row in rows if row.id == 124]) == CancellationDecision.REMOVAL_CANDIDATE


def test_courier_selection_uses_branch_and_activity(farmax_stack):
    repo, engine = farmax_stack
    assert len(repo.fetch_deliverymen()) == 1
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDEDORES SET STATUS='I' WHERE CD_VENDEDOR=5"))
    assert repo.fetch_deliverymen() == []
