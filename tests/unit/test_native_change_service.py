from types import SimpleNamespace

import pytest

from services.native_change_feed_service import FeedSignals, NativeChangeFeedService


@pytest.fixture
def feed(qtbot, monkeypatch):
    workers, publications, errors = [], [], []
    gateway = SimpleNamespace(generation=1, is_ready=lambda: True,
                              config=SimpleNamespace(native=SimpleNamespace(reconcile_interval_ms=60000)))
    journal = SimpleNamespace(journal=object())
    reconciler = SimpleNamespace(_publish=lambda *args: publications.append(args),
                                 sync_error=SimpleNamespace(emit=errors.append))
    def create(*args):
        value = SimpleNamespace(signals=FeedSignals())
        workers.append(value)
        return value
    monkeypatch.setattr("services.native_change_feed_service.NativeFeedWorker", create)
    monkeypatch.setattr("services.native_change_feed_service.QThreadPool.globalInstance",
                        lambda: SimpleNamespace(start=lambda _: None))
    service = NativeChangeFeedService(gateway, journal, reconciler)
    yield service, workers, publications, errors, gateway
    service.stop()


def test_notifications_coalesce_during_worker_then_drain_once(feed, qtbot):
    service, workers, publications, _, _ = feed
    service.start()
    for _ in range(100):
        service.trigger()
    assert len(workers) == 1
    workers[0].signals.result.emit(dict(publications=[(["state"], ["row"])], more=False))
    qtbot.waitUntil(lambda: len(workers) == 2)
    workers[1].signals.result.emit(dict(publications=[], more=False))
    assert publications == [(["state"], ["row"])]
    assert not service.pending and not service.busy


def test_stop_restart_drains_obsolete_callback_without_publication(feed, qtbot):
    service, workers, publications, _, _ = feed
    service.start()
    service.stop()
    service.start()
    assert len(workers) == 1
    workers[0].signals.result.emit(dict(publications=[(["obsolete"], [])], more=False))
    qtbot.waitUntil(lambda: len(workers) == 2)
    assert publications == []
    workers[1].signals.result.emit(dict(publications=[], more=False))


def test_failure_backoff_is_not_bypassed_by_notification_burst(feed):
    service, workers, _, errors, _ = feed
    service.start()
    workers[0].signals.failed.emit("offline")
    for _ in range(100):
        service.trigger()
    assert errors == ["offline"] and len(workers) == 1
    assert service.followup.interval() == 60000


def test_gateway_change_fences_queued_result(feed, qtbot):
    service, workers, publications, _, gateway = feed
    service.start()
    gateway.generation += 1
    workers[0].signals.result.emit(dict(publications=[(["obsolete"], [])], more=True))
    qtbot.waitUntil(lambda: len(workers) == 2)
    assert not publications
