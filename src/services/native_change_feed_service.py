"""Replay and bounded source recovery; WebSockets only accelerate this worker."""
import asyncio

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from api.velide_gateway import freeze_gateway


class FeedSignals(QObject):
    result = pyqtSignal(object)
    failed = pyqtSignal(str)


class NativeFeedWorker(QRunnable):
    def __init__(self, gateway, journal, current):
        super().__init__()
        self.gateway, self.journal, self.current = freeze_gateway(gateway), journal, current
        self.signals = FeedSignals()

    def valid(self):
        return self.current() and self.gateway.is_ready()

    def run(self):
        try:
            self.signals.result.emit(asyncio.run(self.reconcile()))
        except Exception as error:
            self.signals.failed.emit(str(error))

    async def reconcile(self):
        client = self.gateway.get_client()
        if not self.valid() or client is None:
            return None
        publications = []
        async with client:
            state = self.journal.feed_status()
            boundary = state["resync_boundary"]
            if boundary is not None:
                states = await client.list_orders(state["resync_after"])
                if not self.valid():
                    return None
                rows = self.journal.native_states(states, True)
                publications.append((states, rows))
                # Persist rows before advancing this resumable inventory boundary.
                self.journal.resync_progress(boundary, states[-1]["externalOrderId"] if states else "",
                                             len(states) < 100)
                return dict(publications=publications, more=True)
            page = await client.order_changes(state["cursor"] if state["initialized"] else None)
            if not self.valid():
                return None
            self.journal.accept_change_page(page)
            if page["resyncRequired"]:
                return dict(publications=publications, more=True)
            dirty = self.journal.claim_dirty_orders()
            if dirty:
                states = await client.order_states([row["internal_id"] for row in dirty])
                if not self.valid():
                    return None
                rows = self.journal.native_states(states, True, dirty)
                publications.append((states, rows))
            remaining = self.journal.dirty_orders(due_only=True)
            return dict(publications=publications, more=page["eventSequence"] != page["headSequence"]
                        or bool(remaining))


class NativeChangeFeedService(QObject):
    progressed = pyqtSignal()

    def __init__(self, gateway, journal, reconciler):
        super().__init__()
        self.gateway, self.journal, self.reconciler = gateway, journal, reconciler
        self.running, self.busy, self.pending = False, False, False
        self.generation = 0
        self.active = None
        self.timer = QTimer(self)
        self.timer.setInterval(gateway.config.native.reconcile_interval_ms)
        self.timer.timeout.connect(self.trigger)
        self.followup = QTimer(self)
        self.followup.setSingleShot(True)
        self.followup.timeout.connect(self.trigger)

    def start(self):
        if self.running:
            return
        self.running = True
        self.generation += 1
        self.timer.start()
        self.trigger()

    def stop(self):
        self.running = False
        self.generation += 1
        self.pending = False
        self.timer.stop()
        self.followup.stop()
        # Drain an existing worker before its replacement; never overlap writers.

    def trigger(self):
        if not self.running:
            return
        self.pending = True
        if self.busy or self.followup.isActive() or not self.gateway.is_ready():
            return
        self.pending = False
        self.busy = True
        ticket = (self.generation, self.gateway.generation)
        self.active = ticket
        worker = NativeFeedWorker(self.gateway, self.journal.journal,
                                  lambda: self.running and ticket == (self.generation, self.gateway.generation))
        worker.signals.result.connect(lambda result: self.finished(ticket, result))
        worker.signals.failed.connect(lambda error: self.finished(ticket, None, error))
        QThreadPool.globalInstance().start(worker)

    def finished(self, ticket, result, error=None):
        if ticket != self.active:
            return
        self.active = None
        self.busy = False
        if not self.running:
            return
        if ticket != (self.generation, self.gateway.generation):
            self.pending = True
            # The obsolete worker has drained; schedule the current lifecycle's
            # replacement even though its ticket intentionally differs.
            self.followup.start(100)
            return
        elif error:
            self.reconciler.sync_error.emit(error)
            # Notification bursts must not cause a retry storm during an outage.
            if self.running and ticket == (self.generation, self.gateway.generation):
                self.followup.start(self.timer.interval())
            return
        elif result:
            for states, rows in result["publications"]:
                self.reconciler._publish(states, rows)
                if not self.running or ticket != (self.generation, self.gateway.generation):
                    return
            self.pending = self.pending or result["more"]
            self.progressed.emit()
        if (self.running and ticket == (self.generation, self.gateway.generation)
                and self.pending):
            self.followup.start(100)
