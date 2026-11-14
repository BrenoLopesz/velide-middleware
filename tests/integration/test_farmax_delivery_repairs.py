"""Delivery projection/cancellation contracts on the isolated relevant schema."""
from datetime import time

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from connectors.farmax.cancellation import cancellation_progress
from connectors.farmax.errors import FarmaxEvidenceError, FarmaxMappingMissing, FarmaxWaitingError


def payload(repo, operation="DONE", **changes):
    value = dict(source_context=repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context,
                 operation=operation, courier_local_id="5", started_at="2026-11-11T15:00:00Z",
                 completed_at="2026-11-11T16:00:00Z" if operation == "DONE" else None)
    value.update(changes)
    return value


def pending(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',CD_ENTREGADOR=NULL,HORA_SAIDA=NULL"))


def test_departure_from_pending_and_replay_are_idempotent(farmax_stack):
    repo, engine = farmax_stack
    pending(engine)
    request = payload(repo, "IN_ROUTE")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert len([sql for sql in statements if sql.startswith("UPDATE")]) == 1
    statements.clear()
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert not any(sql.startswith("UPDATE") for sql in statements)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_SAIDA,CD_ENTREGADOR FROM ENTREGAS")).fetchone() == ("R", "12:00:00", 5)
        assert conn.execute(text("SELECT STATUS FROM VENDAS WHERE ID_PRODUTO=101")).scalar() == "S"


def test_done_can_repair_missed_departure_in_native_table_order(farmax_stack):
    repo, engine = farmax_stack
    pending(engine)
    request = payload(repo)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    updates = [sql for sql in statements if sql.startswith("UPDATE")]
    assert updates[0].startswith("UPDATE ENTREGAS")
    assert len(updates) == 3 and all(sql.startswith("UPDATE VENDAS") for sql in updates[1:])
    assert all("CD_FILIAL=:branch" in sql or "CD_FILIAL=?" in sql for sql in updates)
    delivery_index = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE ENTREGAS"))
    assert any(sql.startswith("SELECT") and "FROM VENDAS" in sql for sql in statements[delivery_index + 1:])
    statements.clear()
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    assert not any(sql.startswith("UPDATE") for sql in statements)


@pytest.mark.parametrize("cancelled_status", ["C", "D", "I"])
def test_partial_items_remain_excluded_and_unfinished_checkout_is_preserved(farmax_stack, cancelled_status):
    repo, engine = farmax_stack
    pending(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS=:status WHERE ID_PRODUTO=101"), {"status": cancelled_status})
        conn.execute(text("UPDATE VENDAS SET CONCLUIDO='N' WHERE ID_PRODUTO=102"))
    assert len(repo.fetch_deliveries_by_id((123,))) == 1
    assert repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CONCLUIDO,HORAFINAL FROM VENDAS ORDER BY ORDEM")).fetchall() == [
            (cancelled_status, "S", "11:05:00"), ("V", "N", "11:05:00")]


def test_existing_arrival_preserved_and_manual_return_not_a_velide_completion(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',HORA_CHEGADA='12:55:00'"))
    with pytest.raises(FarmaxWaitingError, match="authoritative Velide"):
        repo.apply_delivery_effect(123, payload(repo, "IN_ROUTE"), "America/Sao_Paulo")
    assert repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT HORA_CHEGADA FROM ENTREGAS")).scalar() == "12:55:00"


def test_scheduled_overnight_route_stores_original_clocks_without_rewriting_dates(farmax_stack):
    repo, engine = farmax_stack
    pending(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET DATA='2026-11-12'"))
    assert len(repo.fetch_deliveries_by_id((123,))) == 1
    request = payload(repo, started_at="2026-11-13T02:55:00.123456Z", completed_at="2026-11-13T03:10:00.654321Z")
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT DATA,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (
            "2026-11-12", "23:55:00.123400", "00:10:00.654300")
        assert conn.execute(text("SELECT DATA_CAIXA,HORAFINAL FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == ("2026-11-11", "11:05:00")


@pytest.mark.parametrize("change,error", [
    ({"courier_local_id": None}, FarmaxMappingMissing),
    ({"source_context": None}, FarmaxEvidenceError),
    ({"started_at": "2026-11-11T12:00:00"}, FarmaxEvidenceError),
    ({"completed_at": None}, FarmaxEvidenceError),
    ({"completed_at": "2026-11-11T14:00:00Z"}, FarmaxEvidenceError),
])
def test_invalid_execution_never_mutates_source(farmax_stack, change, error):
    repo, engine = farmax_stack
    request = payload(repo, **change)
    with pytest.raises(error):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", None)


def test_sale_failure_rolls_back_delivery_and_earlier_item(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER FAIL_SECOND BEFORE UPDATE ON VENDAS WHEN OLD.ID_PRODUTO=102 BEGIN SELECT RAISE(ABORT,'injected'); END"))
    with pytest.raises(DBAPIError):
        repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", None)
        assert [row[0] for row in conn.execute(text("SELECT STATUS FROM VENDAS"))] == ["S", "S"]


@pytest.mark.parametrize("sql", [
    "INSERT INTO ENTREGAS SELECT * FROM ENTREGAS",
    "UPDATE VENDAS SET ID_PRODUTO=101,ORDEM=1 WHERE ID_PRODUTO=102",
    "UPDATE VENDAS SET CD_FILIAL=2 WHERE ID_PRODUTO=102",
    "UPDATE VENDAS SET STATUS=NULL WHERE ID_PRODUTO=102",
    "UPDATE VENDAS SET STATUS='C'",
    "UPDATE VENDAS SET TIPO_VENDA='D'",
    "UPDATE VENDAS SET STATUS='X'",
])
def test_conflicting_identity_and_cancelled_sales_do_not_write(farmax_stack, sql):
    repo, engine = farmax_stack
    request = payload(repo)
    with engine.begin() as conn:
        conn.execute(text(sql))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, *args: statements.append(sql))
    with pytest.raises(FarmaxEvidenceError):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")
    assert not any(sql.startswith("UPDATE") for sql in statements)


def test_item_set_change_needs_explicit_journal_adoption(farmax_stack):
    repo, engine = farmax_stack
    original = payload(repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET ORDEM=3 WHERE ID_PRODUTO=102"))
    with pytest.raises(FarmaxWaitingError, match="item set"):
        repo.apply_delivery_effect(123, original, "America/Sao_Paulo")
    adopted = payload(repo)
    assert repo.apply_delivery_effect(123, adopted, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"


def test_context_for_another_sale_cannot_be_reused(farmax_stack):
    repo, engine = farmax_stack
    request = payload(repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_VENDA=124"))
        conn.execute(text("UPDATE VENDAS SET CD_VENDA=124"))
    with pytest.raises(FarmaxEvidenceError, match="Source identity"):
        repo.apply_delivery_effect(124, request, "America/Sao_Paulo")


def test_legacy_delivery_methods_still_write_transactionally(farmax_stack):
    repo, engine = farmax_stack
    pending(engine)
    assert repo.update_delivery_as_in_route(123, 5, time(12)) == "DEPARTURE_FIELDS_MATCH"
    assert repo.update_delivery_as_done(123, time(13)) == "COMPLETED_FIELDS_MATCH"


@pytest.mark.parametrize("statuses", [("I", "I"), ("V", "V"), ("S", "C")])
def test_native_removal_uses_whole_sale_and_committed_delete_log(farmax_stack, statuses):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        for position, status in enumerate(statuses, 1):
            conn.execute(text("UPDATE VENDAS SET STATUS=:status WHERE ORDEM=:position"), {"status": status, "position": position})
        conn.execute(text("DELETE FROM ENTREGAS"))
    assert cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))["decision"] == "UNRESOLVED"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO DELIVERYLOG VALUES(1,123,'DELETE','2026-11-11 12:00:00')"))
    progress = cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))
    assert progress["decision"] == "CANCEL"
    assert progress["items"][0]["removal_evidence"][0]["action"] == "DELETE"


@pytest.mark.parametrize("sql", [
    "UPDATE VENDAS SET CD_FILIAL=2 WHERE ORDEM=2",
    "UPDATE VENDAS SET ID_PRODUTO=101,ORDEM=1 WHERE ORDEM=2",
    "UPDATE VENDAS SET STATUS=NULL WHERE ORDEM=2",
    "UPDATE VENDAS SET ID_PRODUTO=NULL WHERE ORDEM=2",
    "DELETE FROM VENDAS",
])
def test_cancellation_does_not_discard_invalid_rows(farmax_stack, sql):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
        conn.execute(text(sql))
    progress = cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))
    assert progress["decision"] == "UNRESOLVED"
    assert progress["percentage"] != 100


def test_old_delete_log_is_not_evidence_for_a_new_sale(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ENTREGAS"))
        conn.execute(text("INSERT INTO DELIVERYLOG VALUES(1,123,'DELETE','2026-11-10 12:00:00')"))
    assert cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))["decision"] == "UNRESOLVED"


def test_cumulative_cancellation_reads_complete_state_after_new_repository(farmax_stack):
    from connectors.farmax.farmax_repository import FarmaxRepository
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C' WHERE ORDEM=1"))
    partial = cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))
    assert partial["percentage"] == 50 and partial["decision"] == "ACTIVE"
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C',TIPO_VENDA='D' WHERE ORDEM=2"))
    final = cancellation_progress(FarmaxRepository(engine).fetch_sales_statuses_by_id((123,)))
    assert final["percentage"] == 100 and final["decision"] == "CANCEL"


def test_pre_repair_context_without_sale_id_preserves_queued_route(farmax_stack):
    repo, engine = farmax_stack
    pending(engine)
    request = payload(repo, "IN_ROUTE")
    context = request["source_context"]
    del context["sale_id"]
    context["branch_id"] = 1.0
    context["customer_id"] = "10.0"
    context["item_keys"] = [[102.0, 2], [101, "1.0"]]
    # Older/newer producers may retain ancillary fields outside identity.
    context["sale_time"] = "10:59:00"
    context["reference"] = "an earlier delivery instruction"
    context["observer_revision"] = "previous-build"
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    assert "sale_id" not in context  # no mutation of journal-owned payloads
    assert repo.observe_delivery_effect(123, request, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"


@pytest.mark.parametrize("context", [None, {}, {"sale_id": "123"}])
def test_journal_proven_original_mapping_bootstraps_missing_context(farmax_stack, context):
    repo, engine = farmax_stack
    pending(engine)
    request = payload(repo, source_context=context, legacy_adopted=True)
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == (
            "V", 5, "12:00:00", "13:00:00", 90)


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_unproven_missing_context_is_not_implicitly_adopted(farmax_stack, flag):
    repo, engine = farmax_stack
    request = payload(repo, source_context=None, legacy_adopted=flag)
    with pytest.raises(FarmaxEvidenceError, match="provenance is missing"):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", None)


@pytest.mark.parametrize("field,value", [("branch_id", "2"), ("sale_id", "124"), ("customer_id", "11"),
                                        ("sale_date", "2026-11-10"), ("delivery_date", "2026-11-12"),
                                        ("address", "Another customer's address")])
def test_adoption_never_overrides_supplied_conflicting_identity(farmax_stack, field, value):
    repo, _ = farmax_stack
    request = payload(repo, legacy_adopted=True)
    request["source_context"][field] = value
    with pytest.raises(FarmaxEvidenceError, match="Source identity"):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")


def test_adopted_original_still_rejects_ambiguous_live_identity(farmax_stack):
    repo, engine = farmax_stack
    request = payload(repo, source_context=None, legacy_adopted=True)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ENTREGAS SELECT * FROM ENTREGAS"))
    with pytest.raises(FarmaxEvidenceError, match="exactly one delivery"):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")


@pytest.mark.parametrize("has_arrival", [False, True])
def test_velide_departure_replaces_manual_farmax_route_fields(farmax_stack, has_arrival):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_ENTREGADOR=99,HORA_SAIDA='11:45:00',HORA_CHEGADA=:arrival"),
                     {"arrival": "12:55:00" if has_arrival else None})
    route = payload(repo, "IN_ROUTE")
    assert repo.apply_delivery_effect(123, route, "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (
            "R", 5, "12:00:00", "12:55:00" if has_arrival else None)
    done = payload(repo)
    assert repo.apply_delivery_effect(123, done, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == (
            "V", "12:55:00" if has_arrival else "13:00:00", 90)


def test_velide_done_directly_repairs_manual_departure(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_ENTREGADOR=99,HORA_SAIDA='11:45:00'"))
    assert repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (5, "12:00:00", "13:00:00")


def test_closed_native_departure_is_retained_without_holding_velide_done(farmax_stack):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',CD_ENTREGADOR=99,HORA_SAIDA='11:45:00',HORA_CHEGADA='12:55:00'"))
    assert repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (
            "V", 99, "11:45:00", "12:55:00")
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [("V",), ("V",)]


@pytest.mark.parametrize("total,sale_type,concluded,delivery_flag", [
    (0, "V", "N", "S"), (-10, "E", "N", "S"), (None, "B", None, None), (0, None, "N", "N"),
])
def test_existing_pending_delivery_not_gated_by_checkout_list_fields(farmax_stack, total, sale_type, concluded, delivery_flag):
    repo, engine = farmax_stack
    pending(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C' WHERE ID_PRODUTO=101"))
        conn.execute(text("UPDATE VENDAS SET PRECO_TOTAL=:total,TIPO_VENDA=:sale_type,CONCLUIDO=:concluded,ENTREGA=:delivery_flag WHERE ID_PRODUTO=102"),
                     dict(total=total, sale_type=sale_type, concluded=concluded, delivery_flag=delivery_flag))
    assert len(repo.fetch_deliveries_by_id((123,))) == 1
    assert repo.apply_delivery_effect(123, payload(repo), "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM VENDAS WHERE ID_PRODUTO=101")).scalar() == "C"
        assert conn.execute(text("SELECT STATUS,PRECO_TOTAL,TIPO_VENDA,CONCLUIDO,ENTREGA,HORAFINAL FROM VENDAS WHERE ID_PRODUTO=102")).fetchone() == (
            "V", total, sale_type, concluded, delivery_flag, "11:05:00")


def test_removed_cancelled_item_resumes_after_correlated_key_refresh(farmax_stack):
    repo, engine = farmax_stack
    request = payload(repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C' WHERE ID_PRODUTO=101"))
    # Ordinary cancellation state changes do not change retained identity.
    assert repo.apply_delivery_effect(123, dict(request, operation="IN_ROUTE", completed_at=None), "America/Sao_Paulo") == "DEPARTURE_FIELDS_MATCH"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM VENDAS WHERE STATUS='C'"))
    with pytest.raises(FarmaxWaitingError, match="correlated source item set"):
        repo.apply_delivery_effect(123, request, "America/Sao_Paulo")
    progress = cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))
    assert progress["snapshot_complete"] and progress["decision"] == "ACTIVE"
    # Main journal retains the old keys in its evidence history, and adopts the
    # current keys for writes only after correlating stable source identity.
    request["source_context"] = progress["items"][0]["source_context"]
    assert repo.apply_delivery_effect(123, request, "America/Sao_Paulo") == "COMPLETED_FIELDS_MATCH"
