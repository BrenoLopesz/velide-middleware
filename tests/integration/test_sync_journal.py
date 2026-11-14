import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from api.sqlite_manager import SQLiteManager, DeliveryStatus
from api.sync_journal import SyncJournal
from models.velide_delivery_models import Order
from services.sync_journal_service import SyncJournalService
from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
from workers.sqlite_worker import SQLiteWorker, SQLiteWorkerSignals
from workers.native_api_worker import NativeSignals


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
    assert json.loads(journal.restore()[0]["payload"])["address"] == payload()["address"]
    assert json.loads(journal.restore()[0]["payload"])["createdAt"] == "2026-11-11T15:00:00+00:00"
    assert journal.restore()[0]["source_review"] is not None


def test_source_progress_commits_with_unresolved_obligation(journal):
    journal.observe_source([("farmax:123", {"sale_id": "123"})], 500)
    state = SyncJournal(journal.path, journal.scope).source_state()
    assert state["cursor"] == 500
    assert len(state["pending"]) == 1
    journal.observe_source([], 200)
    assert journal.source_state()["cursor"] == 500
    journal.resolve_source("farmax:123")
    assert not journal.source_state()["pending"]


def test_effects_retry_in_order_and_deduplicate(journal, remote_state):
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier","5")
    journal.native_states([remote_state()])
    first = journal.next_effect()
    assert json.loads(first["payload"])["started_at"] == "2026-11-11T15:00:00Z"
    journal.effect_result(first["sequence"], "Firebird offline", first["generation"])
    assert journal.next_effect() is None  # completion cannot overtake departure
    journal.effect_result(first["sequence"], generation=first["generation"], observation="DEPARTURE_FIELDS_MATCH")
    assert journal.next_effect()["operation"] == "DONE"


def test_unversioned_event_never_authorizes_source_work(journal, remote_state):
    journal.native_states([remote_state()])
    original = journal.next_effect()
    journal.enqueue_effect("123", "old:IN_ROUTE", "IN_ROUTE", {"occurred_at": "2026-11-10T15:00Z"})
    assert journal.next_effect() == original  # Unversioned input cannot replace native authority.
    journal.native_states([remote_state()])
    assert journal.next_effect()["effect_key"] == "route:IN_ROUTE"


def test_existing_mappings_survive_upgrade_and_cleanup(journal):
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier", "5")
        db.add_delivery_mapping("remote", "123", DeliveryStatus.DELIVERED)
        db._get_conn().execute("UPDATE DeliveryMapping SET create_at='2000-01-01'")
    assert journal.restore()[0]["external_id"] == "remote"
    with SQLiteManager(journal.path) as db:
        assert db.get_all_mappings() == [("courier", "5")]
        assert db.prune_old_deliveries(0) == 0


def test_courier_mapping_requires_explicit_one_to_one_changes(journal):
    with SQLiteManager(journal.path) as db:
        db.add_many_mappings([("first", "1"), ("second", "2")])
    with pytest.raises(ValueError, match="one-to-one"):
        with SQLiteManager(journal.path) as db:
            db.add_many_mappings([("first", "2")])
    with SQLiteManager(journal.path) as db:
        assert dict(db.get_all_mappings()) == {"first": "1", "second": "2"}
        db.add_many_mappings([("first", "2"), ("second", "1")])
    with SQLiteManager(journal.path) as db:
        assert dict(db.get_all_mappings()) == {"first": "2", "second": "1"}


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
    gateway.config.native = True
    gateway.generation = 1
    gateway.get_client.return_value._build_variables_to_add_delivery.return_value.model_dump.return_value = {"input": {"idempotencyKey": "123"}}
    workers = []

    def make_worker(gateway, operation, *args, **kwargs):
        worker = MagicMock(signals=NativeSignals(), operation=operation, arguments=args)
        workers.append(worker)
        return worker

    monkeypatch.setattr("services.durable_delivery_dispatcher.NativeApiWorker", make_worker)
    monkeypatch.setattr("services.durable_delivery_dispatcher.QThreadPool.globalInstance", lambda: MagicMock())
    first = DurableDeliveryDispatcher(gateway, service)
    first.queue_add("123", Order.model_validate(payload()))
    first.start()
    workers[0].signals.result.emit({"id": "merchant"})
    qtbot.waitUntil(lambda: len(workers) == 2)
    first.stop()  # no result received, SUBMITTING is durable
    second = DurableDeliveryDispatcher(gateway, service)
    outcomes = []
    second.import_accepted.connect(lambda *args: outcomes.append(args))
    second.start()
    workers[2].signals.result.emit({"id": "merchant"})
    qtbot.waitUntil(lambda: len(workers) == 4)
    assert workers[1].arguments == workers[3].arguments
    workers[3].signals.result.emit({"jobId": "job"})
    qtbot.waitUntil(lambda: outcomes == [("123", "job")])
    workers[1].signals.result.emit({"jobId": "stale-job"})
    second.stop()
    service.pool.waitForDone()
    assert service.journal.restore()[0]["job_id"] == "job"
    with SQLiteManager(journal.path) as db:
        assert db.get_delivery_by_internal_id("123") is None


def test_erp_observation_retries_without_mutating_source(journal, qtbot, farmax_stack, remote_state):
    from connectors.farmax.farmax_delivery_updater import FarmaxDeliveryUpdater
    service = SyncJournalService(journal.path, journal.scope)
    repo, _ = farmax_stack
    data = dict(payload(), source_context=repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context)
    journal.accept("123", data)
    journal.native_states([remote_state("IN_TRANSIT")])
    updater = FarmaxDeliveryUpdater(repo, MagicMock(), service, "America/Sao_Paulo")
    updater._timer.setInterval(20)  # Exercise periodic retry without waiting the production five-second interval.
    errors, successes = [], []
    updater.update_failed.connect(lambda *args: errors.append(args))
    updater.update_success.connect(lambda *args: successes.append(args))
    updater.start()
    qtbot.waitUntil(lambda: bool(errors))
    assert not successes
    assert journal.next_effect() is None  # in backoff, not acknowledged
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier", "5")
        db._get_conn().execute("UPDATE SyncEffects SET retry_at=0")
    updater._pump()
    qtbot.waitUntil(lambda: successes == [("123", "IN_ROUTE")])
    updater.stop()
    service.pool.waitForDone()
    assert journal.next_effect() is None
