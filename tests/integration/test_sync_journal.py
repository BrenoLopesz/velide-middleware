import json
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from api.sqlite_manager import SQLiteManager, DeliveryStatus
from api.sync_journal import SyncJournal
from models.velide_delivery_models import Order
from services.sync_journal_service import SyncJournalService
from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
from workers.sqlite_worker import SQLiteWorker, SQLiteWorkerSignals
from workers.velide_worker import VelideWorkerSignals


def payload(internal_id="123"):
    return dict(internal_id=internal_id, customerName="Fixture", address="Rua A, 1",
                createdAt="2026-11-11T12:00:00-03:00")


@pytest.fixture
def journal(tmp_path):
    return SyncJournal(str(tmp_path / "sync.db"), "test")


def test_restart_preserves_frozen_request_and_uncertain_cancellation(journal):
    journal.accept("123", payload())
    request = {"idempotencyKey": "123", "offset": 100000}
    journal.freeze_request("123", request)
    assert journal.claim()["operation"] == "ADD"
    assert journal.cancel("123")["cancellation"] == "REQUESTED"
    restarted = SyncJournal(journal.path, journal.scope)
    row = restarted.claim()
    assert json.loads(row["request"]) == request
    restarted.imported("123", "remote")
    assert restarted.claim()["operation"] == "DELETE"
    restarted.cancelled("123")
    assert restarted.claim() is None
    assert restarted.accept("123", payload())["submission"] == "TERMINAL"


def test_queued_cancellation_is_durable_tombstone(journal):
    journal.accept("123", payload())
    assert journal.cancel("123")["cancellation"] == "ACKNOWLEDGED"
    assert SyncJournal(journal.path, journal.scope).claim() is None


def test_unknown_cancellation_is_not_acknowledged(journal):
    assert journal.cancel("123")["cancellation"] == "REQUESTED"
    assert journal.claim() is None


def test_same_identity_never_changes_accepted_payload(journal):
    journal.accept("123", payload())
    changed = dict(payload(), address="Changed")
    with pytest.raises(ValueError, match="conflicts"):
        journal.accept("123", changed)
    assert json.loads(journal.restore()[0]["payload"]) == payload()


def test_source_progress_commits_with_unresolved_obligation(journal):
    journal.observe_source([("farmax:123", {"sale_id": "123"})], 500)
    state = SyncJournal(journal.path, journal.scope).source_state()
    assert state["cursor"] == 500
    assert len(state["pending"]) == 1
    journal.observe_source([], 200)
    assert journal.source_state()["cursor"] == 500
    journal.resolve_source("farmax:123")
    assert not journal.source_state()["pending"]


def test_effects_retry_in_order_and_deduplicate(journal):
    journal.enqueue_effect("123", "route:start", "IN_ROUTE", {"time": "12:00"})
    journal.enqueue_effect("123", "route:done", "DONE", {"time": "13:00"})
    journal.enqueue_effect("123", "route:start", "IN_ROUTE", {"time": "14:00"})
    first = journal.next_effect()
    assert json.loads(first["payload"])["time"] == "12:00"
    journal.effect_result(first["sequence"], "Firebird offline")
    assert journal.next_effect() is None  # completion cannot overtake departure
    journal.effect_result(first["sequence"])
    assert journal.next_effect()["operation"] == "DONE"


def test_existing_mappings_survive_upgrade_and_cleanup(journal):
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier", "5")
        db.add_delivery_mapping("remote", "123", DeliveryStatus.DELIVERED)
        db._get_conn().execute("UPDATE DeliveryMapping SET create_at='2000-01-01'")
    assert journal.restore()[0]["external_id"] == "remote"
    with SQLiteManager(journal.path) as db:
        assert db.get_all_mappings() == [("courier", "5")]
        assert db.prune_old_deliveries(0) == 0


def test_commit_failure_does_not_emit_success(qtbot, monkeypatch):
    class FailedCommit:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            raise sqlite3.OperationalError("disk full")
        def get_all_deliveries(self):
            return []
    monkeypatch.setattr("workers.sqlite_worker.SQLiteManager", lambda _: FailedCommit())
    signals = SQLiteWorkerSignals()
    results, errors = [], []
    signals.result.connect(results.append)
    signals.error.connect(errors.append)
    SQLiteWorker.for_get_all_deliveries(signals, "unused").run()
    assert not results
    assert len(errors) == 1


def test_dispatcher_restarts_after_response_loss_with_same_key(journal, qtbot, monkeypatch):
    service = SyncJournalService(journal.path, journal.scope)
    gateway = MagicMock()
    gateway.get_client.return_value._build_variables_to_add_delivery.return_value.model_dump.return_value = {"idempotencyKey": "123"}
    workers = []

    def make_worker(gateway, order):
        worker = MagicMock(signals=VelideWorkerSignals(), order=order)
        workers.append(worker)
        return worker

    monkeypatch.setattr("services.durable_delivery_dispatcher.VelideWorker.for_add_delivery", make_worker)
    monkeypatch.setattr("services.durable_delivery_dispatcher.QThreadPool.globalInstance", lambda: MagicMock())
    first = DurableDeliveryDispatcher(gateway, service)
    first.queue_add("123", Order.model_validate(payload()))
    first.start()
    qtbot.waitUntil(lambda: len(workers) == 1)
    first.stop()  # no result received, SUBMITTING is durable
    second = DurableDeliveryDispatcher(gateway, service)
    outcomes = []
    second.delivery_success.connect(lambda *args: outcomes.append(args))
    second.start()
    qtbot.waitUntil(lambda: len(workers) == 2)
    assert workers[0].order.request_variables == workers[1].order.request_variables
    workers[1].signals.delivery_added.emit({"id": "remote"})
    qtbot.waitUntil(lambda: outcomes == [("123", "remote")])
    second.stop()
    service.pool.waitForDone()
    with SQLiteManager(journal.path) as db:
        assert db.get_delivery_by_internal_id("123")[0] == "remote"


def test_erp_retry_preserves_occurrence_time_and_waits_for_ack(journal, qtbot, monkeypatch):
    from connectors.farmax.farmax_delivery_updater import FarmaxDeliveryUpdater
    service = SyncJournalService(journal.path, journal.scope)
    mappings = MagicMock()
    mappings.get_deliveryman_by_external_id.return_value = None
    updater = FarmaxDeliveryUpdater(MagicMock(), mappings, service, "America/Sao_Paulo")
    order = Order.model_validate(payload())
    order.effect_context = {"execution_id": "route", "occurred_at": "2026-11-11T15:30:00+00:00"}
    errors, successes = [], []
    updater.update_failed.connect(lambda *args: errors.append(args))
    updater.update_success.connect(lambda *args: successes.append(args))
    updater.start()
    updater.mark_as_in_route(order, "courier")
    qtbot.waitUntil(lambda: bool(errors))
    assert not successes
    assert journal.next_effect() is None  # in backoff, not acknowledged
    with SQLiteManager(journal.path) as db:
        db._get_conn().execute("UPDATE SyncEffects SET retry_at=0")
    mappings.get_deliveryman_by_external_id.return_value = MagicMock(id="5")
    workers = []
    monkeypatch.setattr(updater._thread_pool, "start", workers.append)
    updater._pump()
    qtbot.waitUntil(lambda: len(workers) == 1)
    assert str(workers[0].kwargs["left_at"]) == "12:30:00"
    workers[0].signals.success.emit(None)
    qtbot.waitUntil(lambda: successes == [("123", "IN_ROUTE")])
    updater.stop()
    service.pool.waitForDone()
    assert journal.next_effect() is None
