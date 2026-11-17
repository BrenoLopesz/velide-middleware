"""Recovery gate regressions with real Qt workers, journal commits and intake."""
import asyncio
import threading

import pytest
import test_rollout_quiescence

from services.native_change_feed_service import NativeChangeFeedService
from utils.fix_asyncio import apply_asyncio_fix

runtime_factory = test_rollout_quiescence.runtime_factory


@pytest.fixture(autouse=True)
def worker_event_loop_policy():
    # Match application startup for real QRunnables on Windows/Python 3.8.
    previous = asyncio.get_event_loop_policy()
    apply_asyncio_fix()
    yield
    asyncio.set_event_loop_policy(previous)


def payload(internal="123"):
    return dict(internal_id=internal, customerName="Fixture", address="Rua A, 1",
                createdAt="2026-11-11T12:00:00-03:00")


def tombstone(internal="123"):
    return dict(externalOrderId=internal, found=True, revision="10", jobId=None,
                importStatus="COMPLETED", deliveryId=None, deliveryStatus=None,
                deleted=False, cancellation="NONE", retainedOutcome="COMPLETED",
                execution=None)


def change_page(after, end, events=(), resync=False):
    return dict(afterSequence=after, eventSequence=end, headSequence=end,
                resyncRequired=resync, events=list(events))


def event(sequence="1", internal="123"):
    return dict(sequence=sequence, externalOrderId=internal, revision="10")


def wire(runtime, monkeypatch, client, starts, claims):
    rec = runtime.services.reconciliation
    runtime.gateway.update_token(test_rollout_quiescence.token())
    monkeypatch.setattr("api.velide_gateway.FrozenGateway.get_client", lambda _: client)
    feed = NativeChangeFeedService(runtime.gateway, runtime.journal, rec)
    rec.change_feed = feed
    feed.progressed.connect(rec.check_recovery)
    # Completion must come from feed progress, not a periodic retry.
    rec.timer.setInterval(60_000)
    feed.timer.setInterval(60_000)
    journal = runtime.journal.journal

    def source_intake():
        starts.append((journal.bootstrap_status(), journal.feed_status(),
                       journal.get_order("123"), journal.dirty_orders()))
        journal.accept("123", payload())
        claims.append(journal.claim(native=True))

    monkeypatch.setattr(runtime.strategy, "start_listening", source_intake)
    deliveries = runtime.services.deliveries
    deliveries._listening = True
    dispatcher = deliveries._dispatcher
    dispatcher._running = dispatcher._authorized = True
    dispatcher._authorized_epoch = runtime.gateway.generation
    return rec, feed, deliveries, dispatcher


@pytest.mark.parametrize("bootstrap_complete", [False, True])
@pytest.mark.parametrize("local_queued", [False, True])
@pytest.mark.parametrize("persisted_dirty", [False, True])
def test_dirty_recovery_blocks_intake_until_authoritative_commit(
        runtime_factory, monkeypatch, qtbot, bootstrap_complete, local_queued, persisted_dirty):
    runtime = runtime_factory(native=True, farmax=True)
    journal = runtime.journal.journal
    if local_queued:
        journal.accept("123", payload())
    journal.bootstrap_progress("", bootstrap_complete)
    journal.accept_change_page(change_page(None, "0", resync=True))
    journal.resync_progress("0", "", True)
    if persisted_dirty:
        journal.accept_change_page(change_page("0", "1", [event()]))
    entered, finish = threading.Event(), threading.Event()
    mutations, starts, claims = [], [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def order_changes(self, after):
            return change_page(after, "1", [event()] if after == "0" else [])

        async def order_states(self, refs):
            entered.set()
            assert finish.wait(3)
            return [tombstone(ref) for ref in refs]

        async def list_orders(self, after):
            assert entered.wait(3)  # Bootstrap observes the dirty identity.
            return [tombstone()] if not after else []

        async def submit_import(self, request):
            mutations.append(request)
            return {"jobId": "unexpected"}

    rec, feed, deliveries, dispatcher = wire(runtime, monkeypatch, Client(), starts, claims)
    try:
        deliveries._scope_ready()
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: rec._initialized and journal.bootstrap_status()["complete"])
        # The ordinary audit may reach order_states before the feed worker.
        qtbot.waitUntil(lambda: not journal.dirty_orders(due_only=True))
        assert not journal.native_recovery_ready()
        assert not starts and not claims and not mutations
        assert not dispatcher._recovery_ready
        assert journal.dirty_orders() == [{"internal_id": "123"}]
        assert journal.dirty_orders(due_only=True) == []
        row = journal.get_order("123")
        assert (row["submission"] == "QUEUED") if local_queued else row is None
        # The cached bootstrap-complete flag cannot bypass the outstanding feed.
        rec.ensure_recovered()
        assert not starts and not dispatcher._recovery_ready
    finally:
        finish.set()
    qtbot.waitUntil(lambda: bool(starts), timeout=3000)
    assert starts[0][0]["complete"] and starts[0][1]["resync_boundary"] is None
    assert starts[0][2]["submission"] == "TERMINAL" and starts[0][3] == []
    assert journal.native_recovery_ready()
    assert claims == [None] and mutations == []
    assert dispatcher._recovery_ready and feed.running
    rec.check_recovery()
    assert len(starts) == 1


@pytest.mark.parametrize("feed_initialized", [False, True])
def test_completed_bootstrap_waits_for_feed_initialization_and_inventory(
        runtime_factory, monkeypatch, qtbot, feed_initialized):
    runtime = runtime_factory(native=True, farmax=True)
    journal = runtime.journal.journal
    journal.bootstrap_progress("123", True)
    journal.accept("123", payload())
    if feed_initialized:
        journal.accept_change_page(change_page(None, "1", resync=True))
    entered, finish = threading.Event(), threading.Event()
    starts, claims = [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def order_changes(self, after):
            if after is None:
                entered.set()
                assert finish.wait(3)
                return change_page(None, "1", resync=True)
            return change_page(after, "1")

        async def list_orders(self, after):
            entered.set()
            assert finish.wait(3)
            return [tombstone()]

        async def order_states(self, refs):
            return [tombstone(ref) for ref in refs]

    rec, _, deliveries, dispatcher = wire(runtime, monkeypatch, Client(), starts, claims)
    try:
        deliveries._scope_ready()
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: rec._initialized)
        assert not journal.native_recovery_ready()
        rec.ensure_recovered()
        assert not starts and not dispatcher._recovery_ready
    finally:
        finish.set()
    qtbot.waitUntil(lambda: bool(starts), timeout=3000)
    assert journal.native_recovery_ready()
    assert starts[0][2]["submission"] == "TERMINAL"
    assert claims == [None] and dispatcher._recovery_ready


def test_restart_and_reannouncement_recheck_persisted_dirty_work(
        runtime_factory, monkeypatch, qtbot):
    runtime = runtime_factory(native=True, farmax=True)
    journal = runtime.journal.journal
    journal.bootstrap_progress("123", True)
    journal.accept_change_page(change_page(None, "0", resync=True))
    journal.resync_progress("0", "", True)
    journal.native_states([tombstone()])
    entered, finish = threading.Event(), threading.Event()
    starts, claims = [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def order_changes(self, after):
            return change_page(after, after)

        async def order_states(self, refs):
            entered.set()
            assert finish.wait(3)
            return [tombstone(ref) for ref in refs]

    rec, _, deliveries, dispatcher = wire(runtime, monkeypatch, Client(), starts, claims)
    deliveries._scope_ready()
    qtbot.waitUntil(lambda: len(starts) == 1)
    rec.stop_service()
    dispatcher._recovery_ready = False
    # A new local queued entry still needs its remote outcome, even though both
    # the previous lifecycle and the persisted inventory were fully recovered.
    journal.accept("456", payload("456"))
    journal.accept_change_page(change_page("0", "1", [event(internal="456")]))
    try:
        deliveries._scope_ready()
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: rec._initialized)
        rec.ensure_recovered()
        assert len(starts) == 1 and not dispatcher._recovery_ready
        assert journal.get_order("456")["submission"] == "QUEUED"
        assert not journal.native_recovery_ready()
    finally:
        finish.set()
    qtbot.waitUntil(lambda: len(starts) == 2)
    assert journal.get_order("456")["submission"] == "TERMINAL"
    assert claims == [None, None] and dispatcher._recovery_ready
