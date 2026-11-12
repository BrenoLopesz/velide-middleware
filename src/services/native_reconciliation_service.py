import json
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, QTimer
from config import TargetSystem
from workers.native_api_worker import NativeApiWorker
from models.velide_delivery_models import Order
from models.delivery_table_model import sync_row_status


class NativeReconciliationService(QObject):
    # Keep presentation wiring shared; native ERP effects come from committed facts.
    delivery_in_route = pyqtSignal(str, str)
    delivery_route_fact = pyqtSignal(str, str, dict)
    delivery_missing = pyqtSignal(str)
    order_state = pyqtSignal(dict)
    order_restored = pyqtSignal(Order, object)
    sync_error = pyqtSignal(str)

    def __init__(self, gateway, journal, target):
        super().__init__()
        self.gateway, self.journal, self.target = gateway, journal, target
        self.timer = QTimer(self)
        self.timer.setInterval(gateway.config.native.reconcile_interval_ms)
        self.timer.timeout.connect(self.trigger_reconciliation)
        self._busy = False
        self._running = False
        self._generation = 0
        self._bootstrap_after = ""
        self._bootstrapped = False
        self._gateway_epoch = gateway.generation
        self._lifecycle = 0
        self._initialized = False
        self._initializing = False

    def invalidate_pending_reads(self):
        self._generation += 1
        self._busy = False
        if self._running:
            QTimer.singleShot(0, self.trigger_reconciliation)

    def start_service(self):
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._lifecycle += 1
        self._initialized = self._initializing = False
        # Periodic recovery is owned by the running service, not a read callback.
        self.timer.start()
        self._load_bootstrap()

    def _load_bootstrap(self):
        if self._initializing or not self._running:
            return
        self._initializing = True
        lifecycle = self._lifecycle
        self.journal.submit("bootstrap_status", success=lambda state: self._bootstrap_loaded(state, lifecycle),
                            failure=lambda error: self._bootstrap_failed(error,lifecycle))

    def _bootstrap_failed(self,error,lifecycle):
        if lifecycle == self._lifecycle and self._running:
            self._initializing = False
            self._error(error)

    def _bootstrap_loaded(self, state, lifecycle):
        if lifecycle == self._lifecycle and self._running:
            self._initializing = False
            self._initialized = True
            self._bootstrap_after, self._bootstrapped = state["after"], state["complete"]
            self.trigger_reconciliation()

    def stop_service(self):
        self._running = False
        self._lifecycle += 1
        self._generation += 1
        self._busy = False
        self.timer.stop()

    def trigger_reconciliation(self):
        if self._running and not self._initialized:
            self._load_bootstrap()
            return
        if self.gateway.generation != self._gateway_epoch:
            self._gateway_epoch = self.gateway.generation
            self._generation += 1
            self._busy = False
        if self._busy or not self._running or not self.gateway.is_ready():
            return
        self._busy = True
        if not self._bootstrapped:
            generation = self._generation
            epoch = self.gateway.generation
            worker = NativeApiWorker(self.gateway, "list_orders", self._bootstrap_after)
            worker.signals.result.connect(lambda states: self._bootstrap_received(states, generation) if epoch == self.gateway.generation else self.invalidate_pending_reads())
            worker.signals.failed.connect(lambda error: self._error(error["message"]) if generation == self._generation and epoch == self.gateway.generation else None)
            QThreadPool.globalInstance().start(worker)
        else:
            generation = self._generation
            self.journal.submit("active_orders", success=lambda rows: self._loaded(rows) if generation == self._generation else None,
                                failure=self._error)

    def _bootstrap_received(self, states, generation):
        if generation != self._generation or not self._running:
            return
        def committed(rows):
            if generation != self._generation or not self._running:
                return
            self._publish(states, rows)
            self._bootstrapped = len(states) < 100
            if states:
                self._bootstrap_after = states[-1]["externalOrderId"]
            self.journal.submit("bootstrap_progress", self._bootstrap_after, self._bootstrapped,
                                success=lambda _: self._bootstrap_page_finished(generation), failure=self._error)
        self.journal.submit("native_states", states, self.target == TargetSystem.FARMAX, success=committed, failure=self._error)

    def _bootstrap_page_finished(self, generation):
        if generation == self._generation and self._running:
            self._busy = False
            self.trigger_reconciliation()

    def _loaded(self, rows):
        ids = [row["internal_id"] for row in rows]
        self._batches = [ids[i:i+100] for i in range(0, len(ids), 100)]
        self._next()

    def _next(self):
        if not self._running or not self._batches:
            self._busy = False
            return
        generation = self._generation
        epoch = self.gateway.generation
        worker = NativeApiWorker(self.gateway, "order_states", self._batches.pop(0))
        worker.signals.result.connect(lambda states: self._received(states, generation) if epoch == self.gateway.generation else self.invalidate_pending_reads())
        worker.signals.failed.connect(lambda error: self._error(error["message"]) if generation == self._generation and epoch == self.gateway.generation else None)
        QThreadPool.globalInstance().start(worker)

    def _received(self, states, generation):
        if generation != self._generation or not self._running:
            return
        self.journal.submit("native_states", states, self.target == TargetSystem.FARMAX,
                            success=lambda rows: self._committed(states, rows, generation), failure=self._error)

    def _committed(self, states, rows, generation):
        if generation != self._generation or not self._running:
            return
        self._publish(states, rows)
        self._next()

    def _publish(self, states, rows):
        for state in states:
            row = next((row for row in rows if row["internal_id"] == state["externalOrderId"]), None)
            if row and row["remote_state"]:
                accepted = json.loads(row["remote_state"])
                if accepted != state:
                    continue  # SQLite rejected an older authoritative response.
                state = dict(accepted, error=row["last_error"] or accepted.get("error"))
            if row and row.get("source_review"):
                state = dict(state, _sync_review=json.loads(row["source_review"]))
            if row and row["payload"] and ((row["submission"] != "TERMINAL" and row["cancellation"] != "ACKNOWLEDGED") or row["last_error"] or row.get("source_review")):
                order = Order.model_validate(json.loads(row["payload"]))
                order.ui_status_hint = sync_row_status(row)
                self.order_restored.emit(order, row["external_id"])
            self.order_state.emit(state)

    def _error(self, error):
        self._busy = False
        self.sync_error.emit(error)
