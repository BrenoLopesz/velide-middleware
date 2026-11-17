"""GUI-thread barrier for the complete installed service graph.

Parent hookup:
    barrier = GraphQuiescence(profile_application.runtimes)
    if barrier.quiesce(time.monotonic() + pause_seconds):
        # Export/apply synchronously on THIS thread while paused.
        # The coordinator holds config.upgrade.lock across the pause. Exporter
        # and consumer use the same FileLock(..., is_singleton=True), reentrantly
        # on this thread. The application retains the installation instance lock.
        ...
    barrier.resume()  # cancellation/failure; may complete asynchronously

The parent must invalidate its inventory attempt BEFORE resume(), block tray
profile selection and shutdown/rebuild during a pause, and retain this object
until resume completes. Runtime views are disabled until resume completes; the
separate controller dialog stays interactive, including Cancel during the drain.
For committed cutover use release(), then retire the paused old graph. Never
export on False, or after the caller's pause deadline.

Journal _effect_lock coordination is acquired ONLY AFTER all global/dedicated
pools, their posted completion callbacks, and ERP effects have drained. Acquiring
it sooner deadlocks workers which need it to record outcomes. Locks are acquired
non-blockingly in canonical path order and held on the GUI thread through export/
application. No pool.clear(), thread termination, or discarded durable writes.
"""
import math
import os
import time

from PyQt5.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QThread, QThreadPool, QTimer, pyqtSignal

from services.sync_journal_service import SyncJournalService
from services.sqlite_service import SQLiteService


def _unique(values):
    return list({id(value): value for value in values if value is not None}.values())


class GraphQuiescence(QObject):
    resumed = pyqtSignal()

    def __init__(self, runtimes):
        super().__init__()
        self.runtimes = list(runtimes.values())
        self.state = "running"
        self.last_error = None
        self._locks = []
        self._paused = []
        self._resume_requested = False
        self._inside_quiesce = False
        self._inside_resume = False
        self._view_states = []
        self._quiet_turns = 0
        self._callback_revision = 0
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(10)
        self._resume_timer.timeout.connect(self._try_resume)
        # Explicit graph edges, including subservices not parented as QObjects.
        components = []
        self._producers = []
        self._updaters = []
        for runtime in self.runtimes:
            services = runtime.services
            strategy = runtime.strategy
            persistence = services.tracking_persistence
            self._producers.extend([
                services.deliveries, services.auth, services.websockets,
                services.reconciliation, services.deliverymen_retriever,
                persistence._cleanup_service, persistence, strategy,
            ])
            components.extend(vars(services).values())
            components.extend([strategy, persistence._cleanup_service,
                               services.deliveries._dispatcher, persistence.journal,
                               persistence._sqlite])
            for name in ("_ingestor", "_status_tracker", "_updater"):
                component = getattr(strategy, name, None)
                components.append(component)
            updater = getattr(strategy, "_updater", None)
            if updater:
                self._updaters.append(updater)
        self._components = _unique(components)
        self._producers = _unique(self._producers)
        self._updaters = _unique(self._updaters)
        self._journals = [item for item in self._components if isinstance(item, SyncJournalService)]
        self._stores = [item for item in self._components if isinstance(item, (SyncJournalService, SQLiteService))]
        self._pools = _unique([QThreadPool.globalInstance()] + [
            value for item in self._components for value in getattr(item, "__dict__", {}).values()
            if isinstance(value, QThreadPool)
        ])

    @property
    def is_quiescent(self):
        return self.state == "quiescent"

    def _check_thread(self):
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() != app.thread() or self.thread() != app.thread():
            raise RuntimeError("GraphQuiescence requires the Qt GUI main thread")

    def _pause(self, component):
        component.pause_for_rollout()
        self._paused.append(component)

    def _disable_views(self):
        for view in _unique(getattr(runtime, "view", None) for runtime in self.runtimes):
            self._view_states.append((view, view.isEnabled()))
            view.setEnabled(False)

    def _restore_views(self):
        states, self._view_states = self._view_states, []
        for view, enabled in states:
            view.setEnabled(enabled)

    def eventFilter(self, receiver, event):
        if event.type() == QEvent.MetaCall:
            self._callback_revision += 1
        return False

    def _turn(self, process_events=True):
        # Runtime views are disabled, but the separate controller dialog must
        # receive real user input so Cancel can interrupt a long drain.
        revision = self._callback_revision
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        if process_events:
            QCoreApplication.processEvents(QEventLoop.AllEvents, 1)
        # processEvents() in a local loop does not deliver DeferredDelete (e.g.
        # completed single-shot timers and signal bridge receivers).
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        # waitForDone(0), unlike activeThreadCount(), includes queued runnables.
        idle = all([pool.waitForDone(0) for pool in self._pools])
        idle = idle and all(not store._callbacks if isinstance(store, SyncJournalService)
                            else not store._pending for store in self._stores)
        # hasPendingEvents() also includes unrelated UI events. Observe queued
        # callbacks instead, including callbacks on Qt's lambda proxy receivers.
        if idle and revision == self._callback_revision:
            self._quiet_turns += 1
        else:
            self._quiet_turns = 0
        return self._quiet_turns >= 2

    def _drain(self, deadline):
        self._quiet_turns = 0
        while time.monotonic() < deadline and not self._resume_requested:
            if self._turn():
                return not self._resume_requested and time.monotonic() < deadline
            time.sleep(min(0.001, max(0, deadline - time.monotonic())))
        return False

    def _stop_effect_pumps(self):
        for updater in self._updaters:
            if updater not in self._paused:
                self._pause(updater)

    def quiesce(self, deadline):
        """Return bool by monotonic deadline; failure leaves a resumable barrier.

        The same object cannot take another snapshot until resume has completed.
        resume() is nonblocking: False means stopping workers are still draining;
        it schedules automatic completion and emits resumed when safe.
        """
        self._check_thread()
        if not math.isfinite(deadline):
            raise ValueError("A finite monotonic deadline is required")
        if self.state != "running" or self._inside_quiesce:
            raise RuntimeError("Resume the previous barrier before quiescing again")
        self.state = "draining"
        self.last_error = None
        self._resume_requested = False
        self._inside_quiesce = True
        QCoreApplication.instance().installEventFilter(self)
        revisions = {store: store.failure_revision for store in self._stores}
        try:
            self._disable_views()
            for component in self._producers:
                self._pause(component)
            # Existing ERP work may continue, but no source/reconcile/dispatch
            # timer can generate an unbounded stream of new obligations.
            for updater in self._updaters:
                updater._pump()
            if not self._drain(deadline):
                return self._fail("deadline or cancellation while draining workers")
            self._stop_effect_pumps()
            effects, errors = [], []
            for journal in self._journals:
                journal.pending_effects(effects.append, errors.append)
            if not self._drain(deadline):
                return self._fail("deadline or cancellation while checking ERP effects")
            if errors or any(effects):
                return self._fail("pending ERP effects remain" if not errors else "ERP effect check failed")
            if any(store.failure_revision != revision for store, revision in revisions.items()):
                return self._fail("a persistence operation failed during drain")
            # In-memory non-durable dispatcher queues cannot be exported.
            if any(getattr(runtime.services.deliveries._dispatcher, "_queue", ())
                   for runtime in self.runtimes):
                return self._fail("non-durable dispatch work remains")
            for journal in sorted(self._journals, key=lambda item: os.path.normcase(os.path.abspath(item.journal.path))):
                lock = journal.journal._effect_lock
                if lock in self._locks:
                    continue
                if time.monotonic() >= deadline or not lock.acquire(blocking=False):
                    return self._fail("journal coordination unavailable before deadline")
                self._locks.append(lock)
            for store in self._stores:
                store._rollout_sealed = True
            self.state = "quiescent"
            return True
        except Exception as error:
            return self._fail(str(error))
        finally:
            self._inside_quiesce = False
            if self._resume_requested:
                self._resume_timer.start()

    def _fail(self, reason):
        self.last_error = reason
        self._stop_effect_pumps()
        self.release()
        self.state = "paused"
        return False

    def release(self):
        """Release journal locks, leaving the old graph paused (committed handoff).

        Does NOT resume writers or reenable views. resume() releases locks too.
        Must be called on the same GUI thread; do not rely on a destructor for
        RLock ownership.
        """
        self._check_thread()
        QCoreApplication.instance().removeEventFilter(self)
        while self._locks:
            self._locks.pop().release()
        if self.state == "quiescent":
            self.state = "paused"

    def resume(self):
        self._check_thread()
        if self.state == "running":
            return True
        self._resume_requested = True
        self.release()
        QCoreApplication.instance().installEventFilter(self)
        if self._inside_quiesce or self._inside_resume:
            return False
        self._stop_effect_pumps()
        self.state = "resuming"
        self._quiet_turns = 0
        if self._try_resume():
            return True
        self._resume_timer.start()
        return False

    def _try_resume(self):
        if self.state == "running":
            return True
        if self._inside_quiesce or self._inside_resume:
            return False
        self._inside_resume = True
        try:
            QCoreApplication.instance().installEventFilter(self)
            # The regular event loop is already running between resume timer
            # ticks. Do not recursively pump native/user events from its timeout.
            if not self._turn(process_events=False):
                return False
            self._resume_timer.stop()
            QCoreApplication.instance().removeEventFilter(self)
            for store in self._stores:
                store._rollout_sealed = False
            # Every old worker/callback has exited before a new generation starts.
            paused, self._paused = self._paused, []
            for component in reversed(paused):
                component.resume_after_rollout()
            self._restore_views()
            self.state = "running"
            self._resume_requested = False
            self.resumed.emit()
            return True
        finally:
            self._inside_resume = False
