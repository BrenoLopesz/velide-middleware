from types import SimpleNamespace
import json
import pytest
from PyQt5.QtCore import QCoreApplication

from api.velide_gateway import VelideGateway
from config import ApiConfig, NativeApiConfig, TargetSystem
from services.native_reconciliation_service import NativeReconciliationService
from workers.native_api_worker import NativeSignals


class DeferredJournal:
    """Release journal callbacks explicitly to exercise asynchronous boundaries."""

    def __init__(self):
        self.journal = SimpleNamespace(owns=lambda _: True)
        self.pending = []
        self.calls = []

    def submit(self, method, *args, success=None, failure=None):
        call = SimpleNamespace(
            method=method, args=args, success=success, failure=failure
        )
        self.pending.append(call)
        self.calls.append(call)

    def take(self, method):
        matching = [call for call in self.pending if call.method == method]
        assert len(matching) == 1
        self.pending.remove(matching[0])
        return matching[0]

    def complete(self, method, result):
        call = self.take(method)
        if method == "native_states":
            result = [dict(internal_id=value["externalOrderId"], remote_state=json.dumps(value),
                           last_error=None, payload=None) for value in call.args[0]]
        call.success(result)

    def count(self, method):
        return sum(call.method == method for call in self.calls)


@pytest.fixture
def reconciler(qtbot, monkeypatch):
    config = ApiConfig(
        velide_server="https://api.invalid/graphql",
        velide_websockets_server="wss://api.invalid/graphql",
        native=NativeApiConfig(
            merchant_id="00000000-0000-0000-0000-000000000001",
            connection_id="00000000-0000-0000-0000-000000000002",
            timezone="America/Sao_Paulo",
            reconcile_interval_ms=60_000,
        ),
    )
    gateway = VelideGateway(config, TargetSystem.FARMAX)
    gateway.update_token("fixture-token")
    workers = []

    def worker_factory(gateway, operation, *args):
        return SimpleNamespace(
            signals=NativeSignals(), operation=operation, args=args
        )

    monkeypatch.setattr(
        "services.native_reconciliation_service.NativeApiWorker", worker_factory
    )
    pool = SimpleNamespace(start=workers.append)
    monkeypatch.setattr(
        "services.native_reconciliation_service.QThreadPool.globalInstance",
        lambda: pool,
    )
    journal = DeferredJournal()
    service = NativeReconciliationService(gateway, journal, TargetSystem.FARMAX)
    errors, published = [], []
    service.sync_error.connect(errors.append)
    service.order_state.connect(lambda value: published.append(
        {key: item for key, item in value.items() if not key.startswith("_")}))
    harness = SimpleNamespace(
        service=service, journal=journal, gateway=gateway, workers=workers,
        errors=errors, published=published,
    )
    yield harness
    service.stop_service()
    QCoreApplication.processEvents()


def start(harness, bootstrapped=True):
    harness.service.start_service()
    harness.journal.complete(
        "bootstrap_status", {"after": "", "complete": bootstrapped}
    )


def load_orders(harness, count=1):
    harness.journal.complete(
        "active_orders", [{"internal_id": str(index)} for index in range(count)]
    )
    return harness.workers[-1] if count else None


def state(revision):
    return {"externalOrderId": "0", "revision": str(revision), "error": None}


def complete_read(harness, worker, revision=1):
    worker.signals.result.emit([state(revision)])
    harness.journal.complete("native_states", [])


@pytest.mark.parametrize("phase", ["active_orders", "order_states", "native_states"])
def test_refresh_burst_during_pass_runs_one_follow_up(reconciler, qtbot, phase):
    h = reconciler
    start(h)
    worker = None
    if phase != "active_orders":
        worker = load_orders(h)
    if phase == "native_states":
        worker.signals.result.emit([state(1)])

    for _ in range(20):
        h.service.trigger_reconciliation()
    assert h.journal.count("active_orders") == 1
    assert len(h.workers) <= 1

    if worker is None:
        worker = load_orders(h)
    if phase != "native_states":
        worker.signals.result.emit([state(1)])
    h.journal.complete("native_states", [])
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)

    complete_read(h, load_orders(h), revision=2)
    QCoreApplication.processEvents()
    assert h.journal.count("active_orders") == 2
    assert len(h.workers) == 2
    assert h.published == [state(1), state(2)]


def test_rejected_unknown_order_is_not_published(reconciler):
    h = reconciler
    start(h)
    load_orders(h).signals.result.emit([state(1)])
    h.journal.take("native_states").success([])
    assert h.published == []


def test_refresh_waits_for_all_batches_before_reloading(reconciler, qtbot):
    h = reconciler
    start(h)
    first = load_orders(h, count=101)
    h.service.trigger_reconciliation()
    complete_read(h, first)
    assert h.journal.count("active_orders") == 1
    assert len(h.workers) == 2
    assert h.workers[-1].args == (["100"],)

    complete_read(h, h.workers[-1], revision=2)
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)
    load_orders(h, count=0)
    QCoreApplication.processEvents()
    assert h.journal.count("active_orders") == 2


def test_refresh_during_empty_order_lookup_is_retained(reconciler, qtbot):
    h = reconciler
    start(h)
    h.service.trigger_reconciliation()
    load_orders(h, count=0)
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)


@pytest.mark.parametrize(
    "phase",
    [
        "bootstrap_status", "active_orders", "order_states",
        "native_states", "bootstrap_progress",
    ],
)
def test_failure_retains_refresh_with_delayed_retry(reconciler, qtbot, phase):
    h = reconciler
    if phase == "bootstrap_status":
        h.service.start_service()
    else:
        start(h, bootstrapped=phase != "bootstrap_progress")
    if phase in ("order_states", "native_states"):
        worker = load_orders(h)
        if phase == "native_states":
            worker.signals.result.emit([state(1)])
    if phase == "bootstrap_progress":
        h.workers[-1].signals.result.emit([])
        h.journal.complete("native_states", [])

    for _ in range(20):
        h.service.trigger_reconciliation()
    if phase == "order_states":
        worker.signals.failed.emit({"message": "temporary failure"})
    else:
        h.journal.take(phase).failure("temporary failure")

    assert h.errors == ["temporary failure"]
    assert h.service._refresh_timer.isActive()
    assert h.service._refresh_timer.interval() == h.service.timer.interval()
    attempts = len(h.journal.calls) + len(h.workers)
    for _ in range(20):
        h.service.trigger_reconciliation()
        QCoreApplication.processEvents()
    assert len(h.journal.calls) + len(h.workers) == attempts

    # Fire the retry deterministically, without a wall-clock sleep.
    h.service._refresh_timer.stop()
    h.service._refresh_timer.timeout.emit()
    qtbot.waitUntil(
        lambda: len(h.journal.calls) + len(h.workers) == attempts + 1
    )


@pytest.mark.parametrize("invalidate", ["mutation", "gateway"])
@pytest.mark.parametrize("outcome", ["result", "failed"])
def test_invalidated_read_drains_before_fresh_pass(
    reconciler, qtbot, invalidate, outcome
):
    h = reconciler
    start(h)
    old = load_orders(h)
    if invalidate == "mutation":
        h.service.invalidate_pending_reads()
    else:
        h.gateway.update_token("replacement-token")
        h.service.trigger_reconciliation()
    QCoreApplication.processEvents()
    assert h.journal.count("active_orders") == 1
    assert len(h.workers) == 1

    if outcome == "result":
        old.signals.result.emit([state(1)])
    else:
        old.signals.failed.emit({"message": "retired session"})
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)
    assert h.journal.count("native_states") == 0
    assert h.errors == []

    complete_read(h, load_orders(h), revision=2)
    QCoreApplication.processEvents()
    assert h.published == [state(2)]
    assert h.journal.count("active_orders") == 2


@pytest.mark.parametrize("phase", ["active_orders", "order_states", "native_states"])
@pytest.mark.parametrize("outcome", ["result", "failed"])
def test_old_callbacks_cannot_release_restarted_pass(
    reconciler, qtbot, phase, outcome
):
    h = reconciler
    start(h)
    if phase == "order_states":
        old = load_orders(h)
    else:
        if phase == "native_states":
            load_orders(h).signals.result.emit([state(1)])
        old = h.journal.take(phase)

    h.service.stop_service()
    start(h)
    if phase == "order_states":
        if outcome == "result":
            old.signals.result.emit([state(1)])
        else:
            old.signals.failed.emit({"message": "old failure"})
    elif outcome == "result":
        old.success([])
    else:
        old.failure("old failure")

    h.service.trigger_reconciliation()
    QCoreApplication.processEvents()
    assert h.errors == []
    assert h.published == []
    assert h.journal.count("active_orders") == 2

    complete_read(h, load_orders(h), revision=2)
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 3)
    assert h.published == [state(2)]


@pytest.mark.parametrize("failed", [False, True])
def test_stop_cancels_queued_follow_up(reconciler, failed):
    h = reconciler
    start(h)
    worker = load_orders(h)
    h.service.trigger_reconciliation()
    if failed:
        worker.signals.failed.emit({"message": "temporary failure"})
    else:
        complete_read(h, worker)
    h.service.stop_service()
    QCoreApplication.processEvents()
    assert h.journal.count("active_orders") == 1

    start(h)
    QCoreApplication.processEvents()
    assert h.journal.count("active_orders") == 2


def test_gateway_change_before_journal_callback_discards_old_response(
    reconciler, qtbot
):
    h = reconciler
    start(h)
    old = h.journal.take("active_orders")
    h.gateway.update_token("replacement-token")
    old.success([{"internal_id": "old-order"}])
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)
    assert h.workers == []
    complete_read(h, load_orders(h), revision=2)
    assert h.published == [state(2)]


def test_scope_change_pauses_work_until_gateway_is_ready(reconciler, qtbot):
    h = reconciler
    start(h)
    old = load_orders(h)
    original = h.gateway.config.native.connection_id
    h.gateway.config.native.connection_id = (
        "00000000-0000-0000-0000-000000000003"
    )
    old.signals.result.emit([state(1)])
    QCoreApplication.processEvents()
    assert h.journal.count("native_states") == 0
    assert h.journal.count("active_orders") == 1

    h.gateway.config.native.connection_id = original
    h.service.trigger_reconciliation()
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 2)
    complete_read(h, load_orders(h), revision=2)
    assert h.published == [state(2)]


def test_bootstrap_refreshes_preserve_pagination_and_initial_audit(
    reconciler, qtbot
):
    h = reconciler
    start(h, bootstrapped=False)
    assert h.workers[0].operation == "list_orders"
    assert h.workers[0].args == ("",)
    for _ in range(20):
        h.service.trigger_reconciliation()
    page = [{"externalOrderId": str(index)} for index in range(100)]
    h.workers[0].signals.result.emit(page)
    h.journal.complete("native_states", [])
    checkpoint = h.journal.take("bootstrap_progress")
    assert checkpoint.args == ("99", False)
    checkpoint.success(None)
    qtbot.waitUntil(lambda: len(h.workers) == 2)
    assert h.workers[1].operation == "list_orders"
    assert h.workers[1].args == ("99",)

    h.service.trigger_reconciliation()
    h.workers[1].signals.result.emit([])
    h.journal.complete("native_states", [])
    checkpoint = h.journal.take("bootstrap_progress")
    assert checkpoint.args == ("99", True)
    checkpoint.success(None)
    qtbot.waitUntil(lambda: h.journal.count("active_orders") == 1)
    load_orders(h, count=0)
    QCoreApplication.processEvents()
    assert len(h.workers) == 2
    assert h.journal.count("active_orders") == 1
