"""Committed reverse projections and replay races, with disposable ERP rows."""
import sqlite3

import pytest
from sqlalchemy import event, text

from api.sqlite_manager import SQLiteManager
from api.sync_journal import SyncJournal
from connectors.farmax.errors import FarmaxEvidenceError, FarmaxWaitingError
from test_restored_write_fences import seeded
from test_firebird_dialect import firebird  # noqa: F401 - disposable localhost fixture


ZONE = "America/Sao_Paulo"


def apply(journal, repo):
    effect = journal.next_effect()
    assert effect
    result = journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    assert journal.effect_result(effect["sequence"], generation=effect["generation"], observation=result)
    return result


def delivery(engine):
    with engine.connect() as conn:
        return tuple(conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone())


@pytest.mark.parametrize("statuses", [
    ["PENDING", "IN_TRANSIT", "PENDING", "IN_TRANSIT"],
    ["IN_TRANSIT", "COMPLETED", "IN_TRANSIT", "COMPLETED"],
    ["IN_TRANSIT", "PENDING", "IN_TRANSIT", "PENDING"],
    ["COMPLETED", "PENDING", "COMPLETED"],
])
def test_committed_undo_redo_converges_and_preserves_original_times(tmp_path, farmax_stack, remote_state, statuses):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    for revision, status in enumerate(statuses, 1):
        state = remote_state(status, revision=str(revision), execution=None) if status == "PENDING" else remote_state(status, revision=str(revision))
        journal.native_states([state])
        apply(journal, repo)
        assert delivery(engine) == {
            "PENDING": ("S", None, None, None),
            "IN_TRANSIT": ("R", 5, "12:00:00", None),
            "COMPLETED": ("V", 5, "12:00:00", "13:00:00"),
        }[status]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [
                ("V" if status == "COMPLETED" else "S",)] * 2
            assert conn.execute(text("SELECT ID_OPERADORCAIXA,VALOR FROM CAIXA")).fetchone() == (90, 30)
        journal.native_states([state])
        assert journal.next_effect() is None


def test_waiting_requires_no_courier_and_restores_tracking(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state()])
    apply(journal, repo)
    with SQLiteManager(journal.path) as db:
        db._get_conn().execute("DELETE FROM DeliverymenMapping")
    journal.native_states([remote_state("PENDING", revision="2", execution=None)])
    apply(journal, repo)
    assert journal.get_order("123")["submission"] == "IMPORTED"
    assert delivery(engine) == ("S", None, None, None)


@pytest.mark.parametrize("native_status", ["V", "C", "D", "I"])
def test_intervening_item_write_even_same_value_is_never_reversed(tmp_path, farmax_stack, remote_state, native_status):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state()])
    apply(journal, repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS=:status WHERE ORDEM=1"), {"status": native_status})
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    result = apply(journal, repo)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [(native_status,), ("S",)]
    assert delivery(engine) == ("R", 5, "12:00:00", None)
    if native_status == "V":
        assert result["outcome"] == "PARTIAL"


def test_preexisting_native_return_is_preserved_as_evidence_not_authority(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',CD_ENTREGADOR=99,HORA_SAIDA='11:00:00',HORA_CHEGADA='11:30:00'"))
        conn.execute(text("UPDATE VENDAS SET STATUS='V'"))
    journal.native_states([remote_state("IN_TRANSIT")])
    result = apply(journal, repo)
    assert result["before"]["cd_entregador"] == 99 and result["outcome"] == "PARTIAL"
    assert delivery(engine) == ("R", 5, "12:00:00", None)


def test_crash_after_erp_commit_preserves_item_provenance(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state()])
    effect = journal.next_effect()
    journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    journal = SyncJournal(journal.path, journal.scope, source_timezone=ZONE)
    apply(journal, repo)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 1
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    apply(journal, repo)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [("S",), ("S",)]


def test_rapid_corrections_replace_unstarted_work_and_reject_stale_ack(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state(revision="1")])
    old = journal.next_effect()
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    journal.native_states([remote_state(revision="3")])
    assert not journal.effect_is_current(old["sequence"], old["generation"])
    apply(journal, repo)
    assert not journal.effect_result(old["sequence"], generation=old["generation"], observation="COMPLETED_FIELDS_MATCH")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 1


def test_periodic_audit_repairs_drift_without_remote_change(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    state = remote_state("IN_TRANSIT")
    journal.native_states([state])
    apply(journal, repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',HORA_CHEGADA='15:00:00'"))
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE SyncEffects SET verified_at=0")
    journal.native_states([state])
    apply(journal, repo)
    assert delivery(engine) == ("R", 5, "12:00:00", None)


@pytest.mark.parametrize("deleted", [True, False])
def test_cancel_delete_restore_does_not_latch_source_receipt(tmp_path, farmax_stack, remote_state, deleted):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING" if deleted else "CANCELLED", deleted=deleted, execution=None)])
    assert journal.get_order("123")["projection_outcome"] == "UNSUPPORTED"
    assert journal.get_order("123")["cancellation"] == "NONE"
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    apply(journal, repo)
    assert delivery(engine)[0] == "R"


def test_known_correction_during_write_rolls_back_delivery_and_receipt(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state()])
    effect = journal.next_effect()
    invalidated = []
    def correction(conn, cursor, statement, *args):
        if not invalidated and statement.startswith("UPDATE ENTREGAS"):
            invalidated.append(True)
            journal.native_states([remote_state("PENDING", revision="2", execution=None)])
    event.listen(engine, "before_cursor_execute", correction)
    with pytest.raises(FarmaxWaitingError):
        journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    assert delivery(engine) == ("R", 5, "12:00:00", None)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 0


def test_feed_cursor_and_dirty_work_survive_restart_and_reject_older_read(tmp_path, farmax_stack, remote_state):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("IN_TRANSIT", revision="5")])
    page = dict(afterSequence="0", eventSequence="1", headSequence="1", resyncRequired=False,
                events=[dict(sequence="1", externalOrderId="123", revision="7")])
    assert journal.accept_change_page(page)
    journal = SyncJournal(journal.path, journal.scope, source_timezone=ZONE)
    assert journal.feed_status()["cursor"] == "1" and journal.dirty_orders()
    claim = journal.claim_dirty_orders()
    journal.native_states([remote_state("COMPLETED", revision="6")], True, claim)
    assert journal.next_effect() is None
    journal.native_states([remote_state("PENDING", revision="7", execution=None)], True, claim)
    assert journal.dirty_orders() == [] and journal.next_effect()["operation"] == "WAITING"


def test_firebird_atomic_receipts_reverse_owned_items_and_keep_native_trigger_effects(tmp_path, firebird, remote_state):  # noqa: F811
    from datetime import time
    repo, engine = firebird
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state()])
    apply(journal, repo)
    with engine.begin() as conn:
        # A native same-value write consumes ownership of this one item.
        conn.execute(text("UPDATE VENDAS SET STATUS='V' WHERE ORDEM=1"))
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    assert apply(journal, repo)["outcome"] == "PARTIAL"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == ("R", time(12), None)
        assert conn.execute(text("SELECT STATUS,CHEQUE,CONCLUIDO,HORAFINAL FROM VENDAS ORDER BY ORDEM")).fetchall() == [
            ("V", "", "S", time(11, 5)), ("S", "", "S", time(11, 5))]
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 2


def test_same_source_cancellation_evidence_cannot_recancel_restored_order(tmp_path, farmax_stack, remote_state):
    from connectors.farmax.cancellation import cancellation_progress
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    progress = cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))
    assert journal.record_farmax_progress("123", progress)["request_cancel"]
    request = journal.claim(native=True)
    request_id = request["cancellation_request_id"]
    journal.native_states([remote_state("CANCELLED", revision="2", execution=None,
                                       cancellation="ACKNOWLEDGED", cancellationRequestId=request_id)])
    journal.native_states([remote_state("PENDING", revision="3", execution=None,
                                       cancellation="SUPERSEDED", cancellationRequestId=request_id)])
    assert journal.get_order("123")["cancellation"] == "NONE"
    assert not journal.cancelled("123", request["attempts"], request_id)
    assert not journal.record_farmax_progress("123", progress)["request_cancel"]
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT disposition FROM SyncCancellationReceipts ORDER BY disposition").fetchall() == [
            ("ACKNOWLEDGED",), ("SUPERSEDED",)]


@pytest.mark.parametrize("backend", ["farmax_stack", "firebird"])
def test_restored_delivery_waits_for_same_sources_commercial_eligibility(
        tmp_path, request, remote_state, backend):
    from connectors.farmax.cancellation import cancellation_progress
    repo, engine = request.getfixturevalue(backend)
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    intent = journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))
    journal.native_states([remote_state("CANCELLED", revision="2", execution=None,
        cancellation="ACKNOWLEDGED", cancellationRequestId=intent["cancellation_request_id"])])
    journal.native_states([remote_state("PENDING", revision="3", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId=intent["cancellation_request_id"])])
    before = delivery(engine)
    effect = journal.next_effect()
    with pytest.raises(FarmaxWaitingError) as pending:
        journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    assert journal.effect_result(effect["sequence"], str(pending.value), effect["generation"],
                                 error_kind=pending.value.kind)
    row = journal.get_order("123")
    assert row["source_review"] is None and row["projection_outcome"] == "PENDING"
    assert delivery(engine) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [("C",), ("C",)]
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 0
    # Only the source operator restores commercial eligibility. No remote
    # revision change or review-hold clearing is needed to retry the same target.
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='S'"))
    active = journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))
    assert active["accepted"] and active["decision"] == "ACTIVE"
    assert journal.retry_work("123")["retry_operation"] == "LOCAL"
    assert apply(journal, repo)["outcome"] == "MATCHED"
    assert delivery(engine) == ("S", None, None, None)


def test_source_identity_conflict_stays_latched_when_items_become_active(tmp_path, farmax_stack, remote_state):
    from connectors.farmax.cancellation import cancellation_progress
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    original_customer = repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context["customer_id"]
    journal.native_states([remote_state("PENDING", execution=None)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_CLIENTE=999"))
        conn.execute(text("UPDATE VENDAS SET CD_CLIENTE=999,STATUS='C'"))
    effect = journal.next_effect()
    with pytest.raises(FarmaxEvidenceError, match="Source identity changed") as conflict:
        journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    assert conflict.value.kind == "SOURCE_CONFLICT"
    assert journal.effect_result(effect["sequence"], str(conflict.value), effect["generation"],
                                 error_kind=conflict.value.kind)
    review = journal.get_order("123")["source_review"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_CLIENTE=:customer"), {"customer": original_customer})
        conn.execute(text("UPDATE VENDAS SET CD_CLIENTE=:customer,STATUS='S'"), {"customer": original_customer})
    assert journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))["accepted"]
    journal.native_states([remote_state("PENDING", revision="2", execution=None)])
    assert journal.get_order("123")["source_review"] == review
    assert journal.next_effect() is None
    with pytest.raises(ValueError, match="Resolve the source review"):
        journal.retry_work("123")


def test_hold_is_a_partial_visibility_result_and_release_reconciles(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None, heldAt="2026-11-11T15:00:00Z")])
    apply(journal, repo)
    assert journal.get_order("123")["projection_outcome"] == "PARTIAL"
    assert delivery(engine)[0] == "S"
    journal.native_states([remote_state("PENDING", revision="2", execution=None, heldAt=None)])
    apply(journal, repo)
    assert journal.get_order("123")["projection_outcome"] == "MATCHED"


@pytest.mark.parametrize("status", ["IN_TRANSIT", "COMPLETED"])
def test_same_status_replaces_execution_courier_and_business_times(tmp_path, farmax_stack, remote_state, status):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state(status)])
    apply(journal, repo)
    with SQLiteManager(journal.path) as db:
        db.add_mapping("second-courier", "7")
    state = remote_state(status, revision="2", route="second-route")
    state["execution"].update(courierId="second-courier", startedAt="2026-11-11T17:00:00Z",
                              completedAt="2026-11-11T18:00:00Z" if status == "COMPLETED" else None)
    journal.native_states([state])
    apply(journal, repo)
    assert delivery(engine) == ("V" if status == "COMPLETED" else "R", 7, "14:00:00",
                                "15:00:00" if status == "COMPLETED" else None)


def test_new_source_request_survives_snapshot_of_older_superseded_receipt(tmp_path, farmax_stack, remote_state):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    journal.cancel("123")
    new_request = journal.claim(native=True)
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
                                       cancellation="SUPERSEDED", cancellationRequestId="older-request")])
    assert journal.dispatch_is_current("123", new_request["attempts"], "DELETE")
    assert journal.get_order("123")["cancellation"] == "REQUESTED"


def test_transport_invalidation_does_not_hide_same_revision_conflict(tmp_path, farmax_stack, remote_state):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("IN_TRANSIT", revision="5")])
    journal.accept_change_page(dict(afterSequence="0", eventSequence="1", headSequence="1", resyncRequired=False,
                                   events=[dict(sequence="1", externalOrderId="123", revision="5")]))
    journal.native_states([remote_state("COMPLETED", revision="5")], True, journal.claim_dirty_orders())
    assert journal.next_effect() is None
    assert "Conflicting lifecycle" in journal.get_order("123")["last_error"]


@pytest.mark.parametrize("relation", ["ENTREGAS", "VENDAS", "VELIDE_PROJECTION_APPLY"])
def test_erp_failure_rolls_back_projection_item_versions_and_receipt(tmp_path, farmax_stack, remote_state, relation):
    from sqlalchemy.exc import DBAPIError
    from connectors.farmax.projection_ledger import ProjectionLedger
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    ProjectionLedger(engine).ensure()
    operation = "INSERT" if relation == "VELIDE_PROJECTION_APPLY" else "UPDATE"
    with engine.begin() as conn:
        conn.execute(text("CREATE TRIGGER FAIL_PROJECTION BEFORE " + operation + " ON " + relation +
                          " BEGIN SELECT RAISE(ABORT,'injected failure'); END"))
    journal.native_states([remote_state()])
    effect = journal.next_effect()
    with pytest.raises(DBAPIError):
        journal.apply_effect(effect["sequence"], effect["generation"], repo, ZONE, lambda: True)
    assert journal.next_effect()["state"] == "PENDING"
    assert delivery(engine) == ("R", 5, "12:00:00", None)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [("S",), ("S",)]
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_PROJECTION_APPLY")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM VELIDE_ITEM_GUARD")).scalar() == 0


def test_completed_order_audit_refreshes_new_item_keys_without_live_tracking(tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    state = remote_state()
    journal.native_states([state])
    apply(journal, repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET ID_PRODUTO=103,ORDEM=3,STATUS='S' WHERE ORDEM=2"))
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE SyncEffects SET verified_at=0")
    journal.native_states([state])
    apply(journal, repo)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT ID_PRODUTO,STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [(101, "V"), (103, "V")]
