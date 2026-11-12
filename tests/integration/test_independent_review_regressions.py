from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QThreadPool
from sqlalchemy import text

from api.sqlite_manager import SQLiteManager
from api.sync_journal import SyncJournal
from api.velide import Velide
from api.velide_gateway import VelideGateway
from config import ApiConfig, TargetSystem
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor, FarmaxIngestorConfig
from connectors.farmax.farmax_delivery_updater import FarmaxDeliveryUpdater
from connectors.farmax.identity import normalize_farmax_id
from models.farmax_models import DeliveryLog
from models.velide_delivery_models import Order
from native_migration import validate_manifest
from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
from services.sync_journal_service import SyncJournalService
from workers.native_api_worker import NativeApiWorker
from workers.velide_worker import VelideWorker


@pytest.fixture
def journal(tmp_path):
    return SyncJournal(str(tmp_path / "review.db"), "test")


def test_f1_real_journal_pipeline_accepts_set_ids_and_resumes_polling(farmax_stack, tmp_path, qtbot):
    repo, engine = farmax_stack
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',CD_ENTREGADOR=NULL,HORA_SAIDA=NULL"))
        conn.execute(text("INSERT INTO DELIVERYLOG VALUES(1,123,'INSERT',:now)"), {"now": datetime.now()})
    journal = SyncJournalService(str(tmp_path / "intake.db"), "test")
    known = set()
    def reserve(value):
        key = normalize_farmax_id(value)
        if key in known:
            return False
        known.add(key)
        return True
    persistence = SimpleNamespace(journal=journal, is_tracked=lambda value: normalize_farmax_id(value) in known, reserve_id=reserve)
    ingestor = FarmaxDeliveryIngestor(repo, persistence, FarmaxIngestorConfig(poll_interval_ms=20))
    received, errors = [], []
    ingestor.orders_received.connect(received.extend)
    ingestor.error_occurred.connect(errors.append)
    ingestor.start()
    try:
        qtbot.waitUntil(lambda: len(received) == 1)
        qtbot.waitUntil(lambda: journal.journal.source_state()["cursor"] == 1)
        assert not errors and received[0].internal_id == "123"
        assert journal.journal.restore()[0]["payload"] is not None
        # UPDATE-only logs return an empty set from the mapper, not a list.
        ingestor._on_logs_retrieved([DeliveryLog(id=2, cd_venda=123, action="UPDATE", logdate=datetime.now())])
        qtbot.waitUntil(lambda: journal.journal.source_state()["cursor"] == 2)
        assert ingestor._poll_timer.isActive()
    finally:
        ingestor.stop()
        QThreadPool.globalInstance().waitForDone()
        journal.pool.waitForDone()


@pytest.mark.parametrize("terminal,changes", [("CANCELLED",{}),("COMPLETED",{"deleted":True}),("PENDING",{"execution":None})])
def test_f2_obsolete_selected_effect_cannot_execute_or_ack(journal, remote_state, terminal, changes):
    journal.native_states([remote_state()])
    selected = journal.next_effect()
    journal.native_states([remote_state(terminal, revision="2", **changes)])
    assert journal.next_effect() is None
    assert journal.prepare_effect(selected["sequence"], selected["generation"]) is None
    assert journal.effect_result(selected["sequence"], generation=selected["generation"], observation="COMPLETED_FIELDS_MATCH") is False


def test_f2_undo_completion_reopens_tracking_and_supersedes_done(journal, remote_state):
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier","5")
    journal.native_states([remote_state()])
    old = journal.next_effect()
    journal.effect_result(old["sequence"], generation=old["generation"], observation="COMPLETED_FIELDS_MATCH")
    assert journal.active_orders() == [{"internal_id":"123"}]
    journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    assert journal.restore()[0]["submission"] == "IMPORTED"
    current = journal.next_effect()
    assert current["operation"] == "IN_ROUTE" and current["generation"] != old["generation"]
    assert journal.effect_result(old["sequence"], generation=old["generation"], observation="COMPLETED_FIELDS_MATCH") is False


def test_f3_stale_execution_and_changed_same_revision_are_not_applied(journal, remote_state):
    journal.native_states([remote_state("IN_TRANSIT", revision="20", route="new")])
    current = journal.next_effect()
    journal.native_states([remote_state("IN_TRANSIT", revision="19", route="old")])
    assert journal.next_effect()["effect_key"] == "new:IN_ROUTE"
    assert journal.next_effect()["generation"] == current["generation"]
    conflicting = remote_state("IN_TRANSIT", revision="20", route="new")
    conflicting["execution"]["startedAt"] = "2026-11-11T14:00:00Z"
    journal.native_states([conflicting])
    assert journal.next_effect() is None
    assert "Conflicting" in journal.restore()[0]["last_error"]
    journal.native_states([conflicting])
    assert journal.next_effect() is None  # Repetition cannot authorize the conflict.


def test_f7_reinstall_matches_existing_final_fields_without_departure_update(farmax_stack, journal, remote_state, qtbot):
    repo, engine = farmax_stack
    context = repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='V',HORA_CHEGADA='13:00:00'"))
        conn.execute(text("UPDATE VENDEDORES SET STATUS='I' WHERE CD_VENDEDOR=5"))
    state = remote_state(source={"customerName":"Fixture","sourceContext":context},
                         request={"rawAddress":"Rua A, 1","createdAtIntent":"2026-11-11T14:00:00Z"})
    journal.native_states([state])
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier","5")
    service = SyncJournalService(journal.path,journal.scope)
    updater = FarmaxDeliveryUpdater(repo, MagicMock(), service)
    matched = []
    updater.update_success.connect(lambda *args: matched.append(args))
    updater.start()
    try:
        qtbot.waitUntil(lambda: matched == [("123","DONE")])
        assert journal.next_effect() is None
        assert journal.restore()[0]["submission"] == "TERMINAL"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0
            assert conn.execute(text("SELECT HORAFINAL,CHEQUE FROM VENDAS WHERE ID_PRODUTO=101")).fetchone() == ("11:05:00","retained")
    finally:
        updater.stop()
        QThreadPool.globalInstance().waitForDone()
        service.pool.waitForDone()


@pytest.mark.parametrize("attempted", [True,False])
def test_f8_only_never_attempted_work_may_be_requeued(journal, attempted):
    journal.accept("123", {"internal_id":"123"})
    if attempted:
        journal.claim()
    manifest = {"version":1,"old_scope":"test","new_scope":"https://new.invalid/graphql|Farmax|00000000-0000-0000-0000-000000000001|00000000-0000-0000-0000-000000000002",
                "deliveries":[{"internal_id":"123","state":"QUEUED"}],"couriers":[]}
    with SQLiteManager(journal.path) as db:
        if attempted:
            with pytest.raises(ValueError, match="never-submitted"):
                validate_manifest(db._get_conn(),manifest)
        else:
            validate_manifest(db._get_conn(),manifest)


def test_f9_queued_workers_do_not_use_replacement_session_credentials(qtbot):
    config = ApiConfig(velide_server="https://api.invalid/", velide_websockets_server="wss://api.invalid/")
    gateway = VelideGateway(config,TargetSystem.FARMAX)
    gateway.update_token("store-A")
    legacy = VelideWorker.for_snapshot(gateway)
    snapshot = gateway.snapshot()
    native = NativeApiWorker(gateway,"validate_scope")
    gateway.update_token("store-B")
    assert snapshot.token == "store-A" and snapshot.get_client() is None
    errors = []
    legacy.signals.error.connect(errors.append)
    native.signals.failed.connect(errors.append)
    legacy.run()
    native.run()
    assert len(errors) == 2  # Neither worker opens a client using store-B.


def test_f9_unverifiable_legacy_store_binding_pauses_dispatch(journal, qtbot):
    config = ApiConfig(velide_server="https://api.invalid/",velide_websockets_server="wss://api.invalid/")
    gateway = VelideGateway(config,TargetSystem.FARMAX)
    gateway.update_token("store-A")
    service = SyncJournalService(journal.path,journal.scope)
    dispatcher = DurableDeliveryDispatcher(gateway,service)
    errors = []
    dispatcher.task_failed.connect(lambda *args: errors.append(args))
    dispatcher.start()
    assert not dispatcher._running and "binding" in errors[0][1]
    dispatcher.stop()
    service.pool.waitForDone()


def test_f14_legacy_request_uses_absolute_source_time_and_frozen_replay():
    config = ApiConfig(velide_server="https://api.invalid/",velide_websockets_server="wss://api.invalid/",source_timezone="America/Sao_Paulo")
    client = Velide("token",config,TargetSystem.FARMAX)
    order = Order(internal_id="123",customerName="Fixture",address="Rua A",createdAt="2026-11-11T23:30:00")
    original = client._build_variables_to_add_delivery(order).model_dump(by_alias=True)
    assert original["offset"] == 0
    assert datetime.fromtimestamp(original["timestamp"]/1000,timezone.utc).isoformat() == "2026-11-12T02:30:00+00:00"
    order.request_variables = original
    assert client._build_variables_to_add_delivery(order).model_dump(by_alias=True) == original
    del original["timestamp"]
    with pytest.raises(ValueError, match="absolute timestamp"):
        client._build_variables_to_add_delivery(order)


def test_unresolved_source_prefix_does_not_starve_later_records(journal):
    journal.observe_source([("farmax:"+str(n),{"sale_id":str(n)}) for n in range(250)])
    first = journal.source_state()["pending"]
    second = journal.source_state()["pending"]
    assert len(first) == len(second) == 100
    assert not {row["source_id"] for row in first}.intersection(row["source_id"] for row in second)
    assert len(journal.source_state()["pending"]) == 50


def test_changed_physical_source_configuration_does_not_reuse_work(tmp_path):
    path = str(tmp_path / "bound-source.db")
    SyncJournal(path,"test",source_identity="source-A").accept("123",{"internal_id":"123"})
    with pytest.raises(ValueError, match="source configuration"):
        SyncJournal(path,"test",source_identity="source-B").restore()


def test_stale_observation_after_source_conflict_is_not_acknowledged(journal,remote_state):
    journal.native_states([remote_state()])
    selected = journal.next_effect()
    journal.source_review("123",{"message":"Sale was removed; review identity"})
    assert journal.next_effect() is None
    assert not journal.effect_result(selected["sequence"], generation=selected["generation"], observation="COMPLETED_FIELDS_MATCH")


def test_changed_mapping_during_observation_cannot_acknowledge(journal, remote_state):
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier","5")
    journal.native_states([remote_state()])
    selected = journal.next_effect()
    with SQLiteManager(journal.path) as db:
        db.add_many_mappings([("courier","6")])
    assert not journal.effect_result(selected["sequence"], generation=selected["generation"], observation="COMPLETED_FIELDS_MATCH")
    assert journal.restore()[0]["submission"] == "IMPORTED"


def test_review_or_cancellation_invalidates_claim_before_transport(journal):
    journal.accept("123", {"internal_id":"123"})
    claim = journal.claim(native=True)
    assert journal.dispatch_is_current("123",claim["attempts"],"ADD")
    journal.source_review("123", {"message":"Physical removal requires review"})
    assert not journal.dispatch_is_current("123",claim["attempts"],"ADD")
    assert journal.claim(native=True) is None
