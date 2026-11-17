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
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.trigger_reconciliation)
        self._refresh_pending = False
        self._busy = False
        self._active_ticket = None
        self._batches = []
        self._running = False
        self._generation = 0
        self._bootstrap_after = ""
        self._bootstrapped = False
        self._gateway_epoch = gateway.generation
        self._lifecycle = 0
        self._initialized = False
        self._initializing = False
        self.change_feed = None

    def invalidate_pending_reads(self):
        self._generation += 1
        if self._running:
            self._refresh_pending = True
            # Let the in-flight callback drain before starting a replacement.
            if not self._busy:
                self._refresh_timer.start(0)

    def start_service(self):
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._lifecycle += 1
        self._initialized = self._initializing = False
        # Periodic recovery is owned by the running service, not a read callback.
        self.timer.start()
        if self.change_feed:
            self.change_feed.start()
        self._load_bootstrap()

    def _load_bootstrap(self):
        if self._initializing or not self._running:
            return
        self._initializing = True
        lifecycle = self._lifecycle
        self.journal.submit(
            "bootstrap_status",
            success=lambda state: self._bootstrap_loaded(state, lifecycle),
            failure=lambda error: self._bootstrap_failed(error, lifecycle))

    def _bootstrap_failed(self, error, lifecycle):
        if lifecycle == self._lifecycle and self._running:
            self._initializing = False
            self._error(error)

    def _bootstrap_loaded(self, state, lifecycle):
        if lifecycle == self._lifecycle and self._running:
            self._initializing = False
            self._initialized = True
            self._bootstrap_after = state["after"]
            self._bootstrapped = state["complete"]
            self.trigger_reconciliation()

    def stop_service(self):
        self._running = False
        self._lifecycle += 1
        self._generation += 1
        self._busy = False
        self._active_ticket = None
        self._batches = []
        self._refresh_pending = False
        self._refresh_timer.stop()
        self.timer.stop()
        if self.change_feed:
            self.change_feed.stop()

    def trigger_reconciliation(self):
        if not self._running:
            return
        self._refresh_pending = True
        if self.gateway.generation != self._gateway_epoch:
            self._gateway_epoch = self.gateway.generation
            self._generation += 1
            self._refresh_timer.stop()
        if self._busy or self._refresh_timer.isActive():
            return
        if not self._initialized:
            self._load_bootstrap()
            return
        if not self.gateway.is_ready():
            return
        self._refresh_pending = False
        self._busy = True
        self._generation += 1
        ticket = (self._lifecycle, self._generation, self.gateway.generation)
        self._active_ticket = ticket
        if not self._bootstrapped:
            worker = NativeApiWorker(self.gateway, "list_orders", self._bootstrap_after)
            worker.signals.result.connect(
                lambda states: self._bootstrap_received(states, ticket))
            worker.signals.failed.connect(
                lambda error: self._read_failed(error["message"], ticket))
            QThreadPool.globalInstance().start(worker)
        else:
            self.journal.submit(
                "active_orders", success=lambda rows: self._loaded(rows, ticket),
                failure=lambda error: self._read_failed(error, ticket))

    def _current(self, ticket):
        if not self._running or ticket != self._active_ticket:
            return False
        expected = (self._lifecycle, self._generation, self.gateway.generation)
        if ticket != expected or not self.gateway.is_ready():
            self._refresh_pending = True
            self._finish_pass()
            return False
        return True

    def _finish_pass(self, delay=0):
        self._busy = False
        self._active_ticket = None
        self._batches = []
        if self._running and self._refresh_pending:
            # Queue rather than recurse; bursts share one follow-up pass.
            self._refresh_timer.start(delay)

    def _read_failed(self, error, ticket):
        if self._current(ticket):
            self._error(error)

    def _bootstrap_received(self, states, ticket):
        if not self._current(ticket):
            return
        def committed(rows):
            if not self._current(ticket):
                return
            self._publish(states, rows)
            if not self._current(ticket):
                return
            self._bootstrapped = len(states) < 100
            if states:
                self._bootstrap_after = states[-1]["externalOrderId"]
            self.journal.submit(
                "bootstrap_progress", self._bootstrap_after, self._bootstrapped,
                success=lambda _: self._bootstrap_page_finished(ticket),
                failure=lambda error: self._read_failed(error, ticket))
        self.journal.submit(
            "native_states", states, self.target == TargetSystem.FARMAX,
            success=committed,
            failure=lambda error: self._read_failed(error, ticket))

    def _bootstrap_page_finished(self, ticket):
        if self._current(ticket):
            self._refresh_pending = True
            self._finish_pass()

    def _loaded(self, rows, ticket):
        if not self._current(ticket):
            return
        ids = [row["internal_id"] for row in rows]
        self._batches = [ids[i:i+100] for i in range(0, len(ids), 100)]
        self._next(ticket)

    def _next(self, ticket):
        if not self._current(ticket):
            return
        if not self._batches:
            self._finish_pass()
            return
        worker = NativeApiWorker(self.gateway, "order_states", self._batches.pop(0))
        worker.signals.result.connect(lambda states: self._received(states, ticket))
        worker.signals.failed.connect(
            lambda error: self._read_failed(error["message"], ticket))
        QThreadPool.globalInstance().start(worker)

    def _received(self, states, ticket):
        if not self._current(ticket):
            return
        self.journal.submit(
            "native_states", states, self.target == TargetSystem.FARMAX,
            success=lambda rows: self._committed(states, rows, ticket),
            failure=lambda error: self._read_failed(error, ticket))

    def _committed(self, states, rows, ticket):
        if not self._current(ticket):
            return
        self._publish(states, rows)
        self._next(ticket)

    def _publish(self, states, rows):
        for state in states:
            if not self.journal.journal.owns(state["externalOrderId"]):
                continue
            row = next(
                (row for row in rows
                 if row["internal_id"] == state["externalOrderId"]), None)
            if row is None:
                continue  # An unknown identity's read may have been invalidated.
            if row and row["remote_state"]:
                accepted = json.loads(row["remote_state"])
                if accepted != state:
                    continue  # SQLite rejected an older authoritative response.
                state = dict(accepted, error=row["last_error"] or accepted.get("error"))
            if row and row.get("source_review"):
                state = dict(state, _sync_review=json.loads(row["source_review"]))
            if row:
                state = dict(state, _projection_outcome=row.get("projection_outcome"),
                             _projection_reason=row.get("projection_reason"))
            if row and row["payload"] and (
                (row["submission"] != "TERMINAL"
                 and row["cancellation"] != "ACKNOWLEDGED")
                or row["last_error"] or row.get("source_review")
                or row.get("projection_outcome") in ("PARTIAL", "UNSUPPORTED")
            ):
                order = Order.model_validate(json.loads(row["payload"]))
                order.ui_status_hint = sync_row_status(row)
                order.sync_details = "Velide: {}{} | Farmax: {}{}".format(
                    "DELETED" if state.get("deleted") else state.get("deliveryStatus") or "UNRESOLVED",
                    " (em espera)" if state.get("heldAt") else "",
                    row.get("projection_outcome") or "PENDING",
                    " — " + row["projection_reason"] if row.get("projection_reason") else "")
                self.order_restored.emit(order, row["external_id"])
            self.order_state.emit(state)

    def _error(self, error):
        # Retain failed work, but a burst of notifications must not spin retries.
        self._refresh_pending = True
        self._finish_pass(delay=self.timer.interval())
        self.sync_error.emit(error)
