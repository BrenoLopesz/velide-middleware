"""Real Qt pools/queued signals and SQLite; remote/ERP boundaries are local doubles."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from PyQt5.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from api.sqlite_manager import SQLiteManager
from api.velide_gateway import VelideGateway
from config import ApiConfig, AuthenticationConfig, FarmaxConfig, NativeApiConfig, TargetSystem
from repositories.deliveries_repository import DeliveryRepository
from services.auth_service import AuthService
from services.deliveries_service import DeliveriesService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from services.native_reconciliation_service import NativeReconciliationService
from services.reconciliation_service import ReconciliationService
from services.rollout_quiescence import GraphQuiescence
from services.sqlite_service import SQLiteService
from services.strategies.cds_strategy import CdsStrategy
from services.strategies.farmax_strategy import FarmaxStrategy
from services.sync_journal_service import SyncJournalService
from services.tracking_persistence_service import TrackingPersistenceService
from services.velide_websockets_service import VelideWebsocketsService
from workers.native_api_worker import NativeSignals


class Signals(QObject):
    done = pyqtSignal()


class Work(QRunnable):
    def __init__(self, run):
        super().__init__()
        self.action = run
        self.signals = Signals()

    def run(self):
        self.action()
        self.signals.done.emit()


def token(sub="test"):
    return jwt.encode({"exp": time.time() + 3600, "sub": sub}, "local-test", algorithm="HS256")


@pytest.fixture
def runtime_factory(tmp_path, qtbot, monkeypatch):
    runtimes = []
    # No native/legacy requests are permitted in these local graph tests.
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network request")
    monkeypatch.setattr("requests.sessions.Session.request", no_network)

    def make(native=False, farmax=False):
        root = tmp_path / str(len(runtimes))
        root.mkdir()
        target = TargetSystem.FARMAX if farmax else TargetSystem.CDS
        api = ApiConfig(velide_server="https://api.invalid/graphql", velide_websockets_server="wss://api.invalid/graphql",
                        native=NativeApiConfig(merchant_id="00000000-0000-4000-8000-000000000001",
                                               connection_id="00000000-0000-4000-8000-000000000002",
                                               timezone="America/Sao_Paulo") if native else None)
        gateway = VelideGateway(api, target)
        auth = AuthService(AuthenticationConfig(domain="auth.invalid", client_id="test", scope="openid", audience="test"),
                           gateway, token_path=str(root / "token"))
        auth._bundle_access_token = token()  # Resume only schedules a future refresh.
        scope = api.velide_server + "|" + target.value
        if native:
            scope += "|" + api.native.merchant_id + "|" + api.native.connection_id
        journal = SyncJournalService(str(root / "journal.db"), scope)
        journal.journal.restore()
        sqlite = SQLiteService(journal.journal.path)
        persistence = TrackingPersistenceService(sqlite, journal=journal)
        deliveries = DeliveriesService(gateway, api, target, DeliveryRepository(), journal)
        rec = (NativeReconciliationService(gateway, journal, target) if native else
               ReconciliationService(gateway, persistence, api, target))
        ws = VelideWebsocketsService(api, auth)
        monkeypatch.setattr(ws, "start_service", lambda: None)
        retriever = DeliverymenRetrieverService(gateway, api, target)
        repo = SimpleNamespace(fetch_pending_ids=lambda **kw: [], fetch_recent_changes=lambda **kw: [],
                               fetch_recent_changes_by_id=lambda **kw: [], fetch_deliveries_by_id=lambda **kw: [],
                               fetch_sales_statuses_by_id=lambda **kw: [])
        strategy = (FarmaxStrategy(FarmaxConfig(host="unused", file="unused", user="unused", password="unused"),
                                   repo, persistence, ws, rec, retriever) if farmax else
                    CdsStrategy(api, str(root), journal))
        deliveries.set_strategy(strategy)
        retriever.set_strategy(strategy)
        if native:
            deliveries.set_recovery_service(rec)
        services = SimpleNamespace(auth=auth, deliveries=deliveries, tracking_persistence=persistence, sqlite=sqlite,
                                   websockets=ws, reconciliation=rec, deliverymen_retriever=retriever)
        runtime = SimpleNamespace(services=services, strategy=strategy, journal=journal, gateway=gateway, root=root)
        runtimes.append(runtime)
        return runtime

    yield make
    for runtime in runtimes:
        runtime.services.auth._refresh_timer.stop()
        runtime.services.reconciliation.stop_service()
        runtime.services.deliveries._dispatcher.stop()
        runtime.strategy.stop_listening()
        runtime.services.tracking_persistence._cleanup_service.stop_routine()
    # Retain receivers until all worker signals and their follow-up writes finish.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        pools = [QThreadPool.globalInstance()] + [runtime.journal.pool for runtime in runtimes]
        if all([pool.waitForDone(0) for pool in pools]):
            QCoreApplication.processEvents()
            if not any(runtime.journal._callbacks for runtime in runtimes):
                break
        time.sleep(0.001)
    assert all([pool.waitForDone(0) for pool in pools])


def barrier(*runtimes):
    return GraphQuiescence({str(i): runtime for i, runtime in enumerate(runtimes)})


def resume(graph, qtbot):
    graph.resume()
    qtbot.waitUntil(lambda: graph.state == "running", timeout=3000)


def test_drains_global_dedicated_pools_and_cascaded_commits_before_locks(runtime_factory, qtbot):
    first, second = runtime_factory(), runtime_factory()
    graph = barrier(first, second)
    outcomes = []
    work = Work(lambda: None)
    work.signals.done.connect(lambda: first.journal.submit("observe_source", [("farmax:1", {"sale_id": "1"})],
        success=lambda _: second.journal.submit("observe_source", [("farmax:2", {"sale_id": "2"})],
            success=lambda _: second.services.sqlite.request_add_mapping("courier", "5"))))
    second.services.sqlite.add_mapping_result.connect(outcomes.append)
    QThreadPool.globalInstance().start(work)
    # A queued hydration callback must not restart daily pruning after pause.
    first.services.tracking_persistence.initialize()
    assert graph.quiesce(time.monotonic() + 3), graph.last_error
    assert outcomes == [True]
    assert not first.journal._callbacks and not second.journal._callbacks
    assert first.journal.journal.source_state()["pending"][0]["source_id"] == "farmax:1"
    assert second.journal.journal.source_state()["pending"][0]["source_id"] == "farmax:2"
    assert not first.services.tracking_persistence._cleanup_service.timer.isActive()
    assert len(graph._locks) == 2
    with pytest.raises(RuntimeError, match="sealed"):
        second.journal.submit("restore")
    graph.release()
    assert not graph.is_quiescent and not graph._locks
    resume(graph, qtbot)
    qtbot.waitUntil(first.services.tracking_persistence._cleanup_service.timer.isActive)


def test_timeout_does_not_drop_queued_writes_or_restart_until_old_worker_exits(runtime_factory, qtbot):
    runtime = runtime_factory()
    graph = barrier(runtime)
    entered, finish = threading.Event(), threading.Event()
    outcomes = []
    def blocked():
        with runtime.journal.journal._effect_lock:
            entered.set()
            assert finish.wait(3)
    runtime.journal.submit(blocked)
    runtime.journal.submit("observe_source", [("farmax:123", {})], success=lambda _: outcomes.append("committed"))
    assert entered.wait(1)
    start = time.monotonic()
    try:
        assert not graph.quiesce(start + 0.04)
        assert time.monotonic() - start < 0.5
        assert not graph._locks
        assert not graph.resume()
        assert runtime.services.auth._rollout_paused
        with pytest.raises(RuntimeError, match="Resume"):
            graph.quiesce(time.monotonic() + 1)
    finally:
        finish.set()
    qtbot.waitUntil(lambda: graph.state == "running", timeout=3000)
    assert outcomes == ["committed"]
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    assert runtime.journal.journal.source_state()["pending"][0]["source_id"] == "farmax:123"
    qtbot.wait(20)
    assert outcomes == ["committed"]  # No old callback crosses the next snapshot.
    graph.release()


def test_lock_contention_fails_without_blocking_gui(runtime_factory):
    runtime = runtime_factory()
    graph = barrier(runtime)
    acquired, finish = threading.Event(), threading.Event()
    def hold():
        with runtime.journal.journal._effect_lock:
            acquired.set()
            finish.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    start = time.monotonic()
    try:
        assert not graph.quiesce(start + 0.2)
        assert "coordination" in graph.last_error
        assert time.monotonic() - start < 0.5
    finally:
        finish.set()
        thread.join(1)


def test_deferred_erp_effects_reject_export_without_erasing_work(runtime_factory, remote_state):
    runtime = runtime_factory(farmax=True)
    runtime.journal.journal.native_states([remote_state("IN_TRANSIT")])
    effect = runtime.journal.journal.next_effect()
    runtime.journal.journal.effect_result(effect["sequence"], "offline", effect["generation"])
    assert runtime.journal.journal.next_effect() is None  # retry_at in the future
    graph = barrier(runtime)
    assert not graph.quiesce(time.monotonic() + 2)
    assert graph.last_error == "pending ERP effects remain"
    with SQLiteManager(runtime.journal.journal.path) as db:
        assert db._get_conn().execute("SELECT state FROM SyncEffects").fetchone()[0] == "PENDING"


def test_inflight_erp_ack_survives_timeout_then_resume(runtime_factory, remote_state, qtbot, farmax_stack):
    runtime = runtime_factory(farmax=True)
    journal = runtime.journal.journal
    repository, _ = farmax_stack
    source = repository.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context
    journal.accept("123", dict(internal_id="123", customerName="Test", address="Rua A",
                               createdAt="2026-11-11T12:00:00-03:00", source_context=source))
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier", "5")
    journal.native_states([remote_state("IN_TRANSIT")])
    entered, finish = threading.Event(), threading.Event()
    def observe(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        assert kwargs["authority_check"]()
        return "DEPARTURE_FIELDS_MATCH"
    runtime.strategy._updater._repository = SimpleNamespace(
        apply_delivery_effect=observe,
        fetch_sales_statuses_by_id=repository.fetch_sales_statuses_by_id)
    outcomes = []
    runtime.strategy._updater.update_success.connect(lambda *args: outcomes.append(args))
    runtime.strategy._updater.start()
    qtbot.waitUntil(entered.is_set)
    graph = barrier(runtime)
    try:
        assert not graph.quiesce(time.monotonic() + 0.03)
        assert not graph.resume()
    finally:
        finish.set()
    qtbot.waitUntil(lambda: graph.state == "running", timeout=3000)
    assert outcomes == [("123", "IN_ROUTE")]
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    with SQLiteManager(journal.path) as db:
        assert db._get_conn().execute("SELECT state FROM SyncEffects").fetchone()[0] == "ACKNOWLEDGED"
    graph.release()


def test_native_feed_read_drains_before_rollout_resume(runtime_factory, remote_state, qtbot):
    from services.native_change_feed_service import NativeChangeFeedService

    runtime = runtime_factory(native=True, farmax=True)
    reconciler = runtime.services.reconciliation
    journal = runtime.journal.journal
    entered, finish = threading.Event(), threading.Event()
    publications = []
    reconciler.order_state.connect(publications.append)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def order_changes(self, after):
            return dict(afterSequence=after or "0", eventSequence="1", headSequence="1",
                        resyncRequired=False, events=[] if after else [
                            dict(sequence="1", externalOrderId="123", revision="10")])

        async def order_states(self, refs):
            entered.set()
            assert finish.wait(3)
            return [remote_state("PENDING", revision="10", execution=None)]

    gateway = SimpleNamespace(
        generation=1, config=runtime.gateway.config,
        is_ready=lambda: True, get_client=Client)
    feed = NativeChangeFeedService(gateway, runtime.journal, reconciler)
    reconciler.change_feed = feed
    reconciler.start_service()
    qtbot.waitUntil(entered.is_set)
    graph = barrier(runtime)
    try:
        assert not graph.quiesce(time.monotonic() + 0.03)
        assert not feed.running and feed.busy
        assert not graph.resume()
        assert graph.state == "resuming"
        assert journal.get_order("123") is None
    finally:
        finish.set()
    qtbot.waitUntil(lambda: graph.state == "running" and not feed.busy)
    assert feed.running
    assert publications == []
    assert journal.get_order("123") is None
    # The drained read cannot consume its durable invalidation. Normal retry
    # scheduling will retrieve the authoritative state in the resumed lifecycle.
    assert journal.dirty_orders() == [{"internal_id": "123"}]


def test_persistence_failure_prevents_snapshot(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    def fail():
        raise OSError("disk full")
    runtime.journal.submit(fail)
    graph = barrier(runtime)
    assert not graph.quiesce(time.monotonic() + 2)
    assert "persistence operation failed" in graph.last_error
    assert not graph._locks


def test_cancellation_during_nested_events_resumes_after_drain(runtime_factory, qtbot):
    runtime = runtime_factory()
    graph = barrier(runtime)
    QTimer.singleShot(0, graph.resume)
    assert not graph.quiesce(time.monotonic() + 2)
    qtbot.waitUntil(lambda: graph.state == "running")
    assert not graph._locks
    assert graph.quiesce(time.monotonic() + 2)
    graph.release()


def test_auth_rotation_commits_during_pause_but_publishes_only_on_resume(runtime_factory, monkeypatch, qtbot):
    from utils.token_storage import read_token_from_file
    runtime = runtime_factory()
    auth = runtime.services.auth
    old_token, new_token = token("old"), token("new")
    auth._on_access_token_received(old_token, "old-refresh")
    epoch = runtime.gateway.generation
    entered, finish = threading.Event(), threading.Event()
    def response(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        return MagicMock(status_code=200, text=json.dumps({"access_token": new_token, "refresh_token": "rotated"}))
    monkeypatch.setattr("workers.refresh_token_worker.requests.post", response)
    auth._manual_refresh_token("old-refresh")
    assert entered.wait(1)
    graph = barrier(runtime)
    QTimer.singleShot(10, finish.set)
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    assert read_token_from_file(auth._token_path)["refresh_token"] == "rotated"
    assert runtime.gateway.generation == epoch
    auth.load_stored_token()  # queued websocket/auth callback cannot spawn a reader
    assert QThreadPool.globalInstance().waitForDone(0)
    resume(graph, qtbot)
    assert auth._current_refresh_token == "rotated"
    assert runtime.gateway.snapshot().token == new_token


@pytest.mark.parametrize("farmax", [False, True])
def test_native_recovery_commits_before_source_intake(runtime_factory, monkeypatch, qtbot, farmax):
    runtime = runtime_factory(native=True, farmax=farmax)
    runtime.gateway.update_token(token())
    entered, finish = threading.Event(), threading.Event()
    operations = []
    class APIWorker(QRunnable):
        def __init__(self, gateway, operation, *args, **kwargs):
            super().__init__()
            self.operation = operation
            self.signals = NativeSignals()
        def run(self):
            operations.append(self.operation)
            if self.operation == "list_orders":
                entered.set()
                assert finish.wait(3)
            self.signals.result.emit([] if self.operation != "validate_scope" else {})
    monkeypatch.setattr("services.durable_delivery_dispatcher.NativeApiWorker", APIWorker)
    monkeypatch.setattr("services.native_reconciliation_service.NativeApiWorker", APIWorker)
    starts = []
    def start_source():
        starts.append(runtime.journal.journal.bootstrap_status())
    monkeypatch.setattr(runtime.strategy, "start_listening", start_source)
    runtime.services.deliveries.start_listening()
    qtbot.waitUntil(entered.is_set)
    try:
        assert starts == []
        assert not runtime.services.deliveries._dispatcher._recovery_ready
    finally:
        finish.set()
    qtbot.waitUntil(lambda: bool(starts))
    assert starts == [{"after": "", "complete": True}]
    assert runtime.services.deliveries._dispatcher._recovery_ready
    assert "submit_import" not in operations
    graph = barrier(runtime)
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    graph.release()


def test_native_without_recovery_hook_fails_closed(runtime_factory, monkeypatch, qtbot):
    runtime = runtime_factory(native=True)
    runtime.services.deliveries._recovery = None
    starts = MagicMock()
    monkeypatch.setattr(runtime.strategy, "start_listening", starts)
    runtime.services.deliveries.start_listening()
    runtime.services.deliveries._scope_ready()
    starts.assert_not_called()
    assert not runtime.services.deliveries._dispatcher._recovery_ready


def test_cds_stop_does_not_join_observer_on_gui_thread(qtbot):
    from workers.cds_logs_listener_worker import CdsLogsListenerWorker
    worker = CdsLogsListenerWorker("unused")
    worker._observer = MagicMock()
    worker.stop()
    worker._observer.stop.assert_not_called()
    assert worker._stop.is_set()


def test_websocket_stop_interrupts_token_wait_and_drains_pool(runtime_factory, monkeypatch, qtbot):
    from workers.velide_websockets_worker import VelideWebsocketsWorker
    runtime = runtime_factory()
    entered = threading.Event()
    async def wait_token(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr("workers.velide_websockets_worker.AsyncTokenProvider.get_valid_token", wait_token)
    ws = runtime.services.websockets
    ws._worker = VelideWebsocketsWorker(ws.api_config, runtime.services.auth)
    ws._worker.signals.finished.connect(ws._on_worker_finished)
    QThreadPool.globalInstance().start(ws._worker)
    qtbot.waitUntil(entered.is_set)
    graph = barrier(runtime)
    assert graph.quiesce(time.monotonic() + 1), graph.last_error
    assert ws._worker is None
    graph.release()


def test_delayed_http_outcome_and_nested_sqlite_callback_are_in_snapshot(runtime_factory, monkeypatch, qtbot):
    from upgrade_inventory import inventory_state_fingerprint
    from upgrade_cds import create_inventory_table
    import sqlite3
    runtime = runtime_factory(native=True)
    journal = runtime.journal.journal
    with SQLiteManager(journal.path) as db:
        create_inventory_table(db._get_conn())
    journal.accept("123", dict(internal_id="123", customerName="Test", address="Rua A",
                               createdAt="2026-11-11T12:00:00-03:00"))
    journal.freeze_request("123", {"input": {"externalOrderId": "123"}})
    runtime.gateway.update_token(token())
    entered, finish = threading.Event(), threading.Event()
    class HTTPWorker(QRunnable):
        def __init__(self, gateway, operation, *args, **kwargs):
            super().__init__()
            self.signals = NativeSignals()
        def run(self):
            entered.set()
            assert finish.wait(3)
            self.signals.result.emit({"jobId": "accepted-before-export"})
    monkeypatch.setattr("services.durable_delivery_dispatcher.NativeApiWorker", HTTPWorker)
    dispatcher = runtime.services.deliveries._dispatcher
    dispatcher._running = dispatcher._authorized = dispatcher._recovery_ready = True
    dispatcher._authorized_epoch = runtime.gateway.generation
    dispatcher.import_accepted.connect(lambda *_: runtime.services.sqlite.request_add_mapping("courier", "5"))
    dispatcher._pump()
    qtbot.waitUntil(entered.is_set)
    graph = barrier(runtime)
    QTimer.singleShot(20, finish.set)
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    assert journal.get_order("123")["job_id"] == "accepted-before-export"
    with SQLiteManager(journal.path) as db:
        assert db.get_all_mappings() == [("courier", "5")]
    def fingerprint():
        conn = sqlite3.connect(journal.path)
        conn.row_factory = sqlite3.Row
        try:
            return inventory_state_fingerprint(conn)
        finally:
            conn.close()
    before = fingerprint()
    # Emulate the parent's nested HTTP event loop while inventory is held.
    qtbot.wait(50)
    assert fingerprint() == before
    assert graph.is_quiescent
    graph.release()


def test_native_pagination_replays_crashed_page_and_gates_dispatch(runtime_factory, monkeypatch, qtbot):
    runtime = runtime_factory(native=True)
    journal = runtime.journal.journal
    journal.accept("fresh", dict(internal_id="fresh", customerName="Test", address="Rua A",
                                 createdAt="2026-11-11T12:00:00-03:00"))
    journal.freeze_request("fresh", {"input": {"externalOrderId": "fresh"}})
    runtime.gateway.update_token(token())
    pages, mutations, starts, errors = [], [], [], []
    def state(index):
        return dict(externalOrderId=str(index).zfill(4), found=True, revision="1",
                    jobId=None, importStatus="COMPLETED", deliveryId=None, deliveryStatus=None,
                    deleted=False, cancellation="NONE", retainedOutcome="COMPLETED", execution=None)
    class APIWorker(QRunnable):
        def __init__(self, gateway, operation, *args, **kwargs):
            super().__init__()
            self.operation, self.args = operation, args
            self.signals = NativeSignals()
        def run(self):
            if self.operation == "list_orders":
                after = self.args[0]
                pages.append(after)
                result = [state(i) for i in (range(100) if after == "" else range(100, 102))]
            elif self.operation == "submit_import":
                mutations.append(journal.bootstrap_status())
                result = {"jobId": "job"}
            else:
                result = []
            self.signals.result.emit(result)
    monkeypatch.setattr("services.durable_delivery_dispatcher.NativeApiWorker", APIWorker)
    monkeypatch.setattr("services.native_reconciliation_service.NativeApiWorker", APIWorker)
    original_progress = journal.bootstrap_progress
    failed = []
    def crash_between_states_and_cursor(after, complete):
        if not failed:
            failed.append(True)
            raise OSError("simulated crash before page cursor commit")
        original_progress(after, complete)
    monkeypatch.setattr(journal, "bootstrap_progress", crash_between_states_and_cursor)
    rec = runtime.services.reconciliation
    rec.sync_error.connect(errors.append)
    monkeypatch.setattr(runtime.strategy, "start_listening",
                        lambda: starts.append((journal.bootstrap_status(), len(journal.restore()))))
    runtime.services.deliveries.start_listening()
    qtbot.waitUntil(lambda: bool(errors))
    assert journal.bootstrap_status() == {"after": "", "complete": False}
    assert len(journal.restore()) == 101
    assert not starts and not mutations
    # A restarted recovery service must reread the durable cursor, replay the
    # committed first 100 identities, and only open intake after the final page.
    rec.stop_service()
    rec.start_service()
    qtbot.waitUntil(lambda: bool(mutations), timeout=3000)
    assert pages == ["", "", "0099"]
    assert starts == [({"after": "0101", "complete": True}, 103)]
    assert mutations == [{"after": "0101", "complete": True}]
    graph = barrier(runtime)
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    graph.release()


def test_additional_dedicated_pool_is_not_mistaken_for_idle(runtime_factory, qtbot):
    runtime = runtime_factory()
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    runtime.strategy._thread_pool = pool
    entered, finish = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert finish.wait(3)
    pool.start(Work(blocked))
    committed = []
    second = Work(lambda: None)
    second.signals.done.connect(lambda: runtime.journal.submit("observe_source", [("farmax:3", {})],
                                                             success=lambda _: committed.append(True)))
    pool.start(second)
    assert entered.wait(1)
    graph = barrier(runtime)
    try:
        assert not graph.quiesce(time.monotonic() + 0.03)
        assert not graph.resume()
    finally:
        finish.set()
    qtbot.waitUntil(lambda: graph.state == "running")
    assert committed == [True]
    assert pool.waitForDone(0)


def test_pre_auth_bootstrap_read_does_not_lose_reconnect_intake_gate(runtime_factory, monkeypatch, qtbot):
    runtime = runtime_factory(native=True)
    runtime.journal.journal.bootstrap_progress("last-page", True)
    starts = []
    monkeypatch.setattr(runtime.strategy, "start_listening", lambda: starts.append(True))
    rec = runtime.services.reconciliation
    # Legacy builder wiring can start this before the auth/scope check.
    rec.start_service()
    qtbot.waitUntil(lambda: rec._initialized)
    assert not starts
    dispatcher = runtime.services.deliveries._dispatcher
    runtime.services.deliveries._listening = True
    dispatcher._authorized = True
    dispatcher._authorized_epoch = runtime.gateway.generation
    runtime.services.deliveries._scope_ready()
    assert starts == [True]
    assert dispatcher._recovery_ready


def test_paused_token_callback_does_not_wait_on_slow_token_storage(runtime_factory, qtbot):
    runtime = runtime_factory()
    auth = runtime.services.auth
    entered, finish = threading.Event(), threading.Event()
    def storage():
        with auth._session_lock:
            entered.set()
            assert finish.wait(3)
    QThreadPool.globalInstance().start(Work(storage))
    assert entered.wait(1)
    graph = barrier(runtime)
    QTimer.singleShot(0, lambda: auth._on_access_token_received(token(), "refresh",
                         auth._session_generation, auth._credential_revision))
    start = time.monotonic()
    try:
        assert not graph.quiesce(start + 0.03)
        assert time.monotonic() - start < 0.5
    finally:
        finish.set()
    resume(graph, qtbot)


def test_native_farmax_hydrates_recovered_tombstones_before_starting_ingestor(runtime_factory, monkeypatch, qtbot):
    runtime = runtime_factory(native=True, farmax=True)
    journal = runtime.journal.journal
    journal.native_states([dict(externalOrderId="123", found=True, revision="1", jobId=None,
        importStatus="COMPLETED", deliveryId=None, deliveryStatus=None, deleted=False,
        cancellation="NONE", retainedOutcome="COMPLETED", execution=None)])
    journal.bootstrap_progress("123", True)
    starts = []
    persistence = runtime.services.tracking_persistence
    monkeypatch.setattr(runtime.strategy._ingestor, "start", lambda: starts.append(persistence.is_archived("123")))
    monkeypatch.setattr(runtime.strategy._status_tracker, "start", lambda: None)
    deliveries = runtime.services.deliveries
    deliveries._listening = True
    deliveries._dispatcher._authorized = True
    deliveries._dispatcher._authorized_epoch = runtime.gateway.generation
    deliveries._scope_ready()
    qtbot.waitUntil(lambda: bool(starts))
    assert starts == [True]
    graph = barrier(runtime)
    assert graph.quiesce(time.monotonic() + 2), graph.last_error
    graph.release()


def test_cancel_dialog_click_interrupts_drain_but_views_wait_for_writer(runtime_factory, qtbot):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDialog, QPushButton, QVBoxLayout, QWidget

    runtime, disabled = runtime_factory(), runtime_factory()
    runtime.view, disabled.view = QWidget(), QWidget()
    qtbot.addWidget(runtime.view)
    qtbot.addWidget(disabled.view)
    layout = QVBoxLayout(runtime.view)
    retry = QPushButton("Retry", runtime.view)
    layout.addWidget(retry)
    disabled.view.setEnabled(False)
    runtime.view.show()

    dialog = QDialog()  # Separate controller dialog, not inside a runtime view.
    qtbot.addWidget(dialog)
    cancel = QPushButton("Cancel", dialog)
    QVBoxLayout(dialog).addWidget(cancel)
    dialog.show()
    qtbot.wait(10)

    entered, finish = threading.Event(), threading.Event()
    def writer():
        entered.set()
        assert finish.wait(3)
    runtime.journal.submit(writer)
    outcomes = []
    runtime.journal.submit("observe_source", [("farmax:cancel-ui", {})],
                           success=lambda _: outcomes.append("committed"))
    assert entered.wait(1)
    graph = barrier(runtime, disabled)
    clicks, resumed = [], []
    retry.clicked.connect(lambda: clicks.append("unsafe retry"))
    def cancel_attempt():
        # Parent's durable invalidation must precede asking the graph to resume.
        (runtime.root / "attempt-state").write_text("INVALIDATED")
        clicks.append(("cancel", graph._inside_quiesce))
        assert not graph.resume()
    cancel.clicked.connect(cancel_attempt)
    graph.resumed.connect(lambda: resumed.append((list(outcomes), runtime.view.isEnabled(),
                                                  disabled.view.isEnabled())))
    def click_controls():
        assert not runtime.view.isEnabled() and not retry.isEnabled()
        assert dialog.isEnabled() and cancel.isEnabled()
        qtbot.mouseClick(retry, Qt.LeftButton)
        qtbot.mouseClick(cancel, Qt.LeftButton)
    QTimer.singleShot(10, click_controls)
    start = time.monotonic()
    try:
        assert not graph.quiesce(start + 2)
        assert time.monotonic() - start < 0.5
        assert clicks == [("cancel", True)]
        assert (runtime.root / "attempt-state").read_text() == "INVALIDATED"
        assert not runtime.view.isEnabled() and not disabled.view.isEnabled()
        assert not resumed and not outcomes
        qtbot.wait(20)
        assert not resumed and not runtime.view.isEnabled()
    finally:
        finish.set()
    qtbot.waitUntil(lambda: graph.state == "running", timeout=3000)
    assert resumed == [(["committed"], True, False)]
    assert graph.quiesce(time.monotonic() + 2)
    graph.release()
    assert not runtime.view.isEnabled()  # Committed handoff keeps old controls off.
    resume(graph, qtbot)
    assert runtime.view.isEnabled() and not disabled.view.isEnabled()
