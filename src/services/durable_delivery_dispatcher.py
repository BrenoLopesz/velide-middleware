import json

from PyQt5.QtCore import QObject, QTimer, QThreadPool, pyqtSignal
from api.velide_gateway import freeze_gateway
from models.velide_delivery_models import Order
from workers.native_api_worker import NativeApiWorker


class DurableDeliveryDispatcher(QObject):
    delivery_success = pyqtSignal(str, str)
    deletion_success = pyqtSignal(str, str)
    task_failed = pyqtSignal(str, str)
    order_restored = pyqtSignal(Order, object)
    import_accepted = pyqtSignal(str, str)
    scope_ready = pyqtSignal()
    scope_invalidated = pyqtSignal()
    mutation_changed = pyqtSignal()

    def __init__(self, gateway, journal):
        super().__init__()
        self.gateway, self.journal = gateway, journal
        self.native = gateway.config.native
        self._running = self._authorized = self._busy = False
        self._generation = 0
        self._authorized_epoch = None
        self._busy_ticket = None
        self._validating = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._pump)

    def _ticket(self):
        return self._generation, self.gateway.generation

    def _current(self, ticket):
        return self._running and ticket == self._ticket()

    def start(self):
        self._generation += 1
        self._running = True
        self._authorized = self._validating = False
        if not self.native:
            # The deployed legacy SDL exposes no authenticated store ID. A
            # URL, location ID, or unverified token claim is not a store binding.
            self._storage_error("Legacy dispatch paused: authoritative store binding is unavailable; migrate to the source-scoped native API")
            return
        self._timer.start()
        self._validate_scope()

    def stop(self):
        self._generation += 1
        self._running = self._authorized = self._validating = False
        self._timer.stop()
        self.scope_invalidated.emit()

    def _validate_scope(self):
        if not self._running or self._validating or not self.gateway.is_ready():
            return
        self._validating = True
        ticket = self._ticket()
        worker = NativeApiWorker(freeze_gateway(self.gateway), "validate_scope")
        worker.signals.result.connect(lambda _: self._validated(ticket))
        worker.signals.failed.connect(lambda error: self._validation_failed(ticket, error))
        QThreadPool.globalInstance().start(worker)

    def _validated(self, ticket):
        if not self._current(ticket):
            self._validating = False
            return
        self._validating = False
        self._authorized = True
        self._authorized_epoch = ticket[1]
        self.scope_ready.emit()
        self.journal.submit("restore", success=lambda rows: self._restored(rows, ticket), failure=self._storage_error)

    def _validation_failed(self, ticket, error):
        self._validating = False
        if not self._current(ticket):
            return
        self.task_failed.emit("", error["message"])
        if not error["retryable"]:
            self.stop()

    def _restored(self, rows, ticket):
        if not self._current(ticket):
            return
        for row in rows:
            if row["payload"] and row["submission"] != "TERMINAL" and row["cancellation"] != "ACKNOWLEDGED":
                self.order_restored.emit(Order.model_validate(json.loads(row["payload"])), row["external_id"])
        self._pump()

    def queue_add(self, internal_id, order):
        payload = order.model_dump(mode="json", by_alias=True)
        payload["internal_id"] = internal_id
        def accepted(_):
            if not self.native:
                self.task_failed.emit(internal_id, "Legacy dispatch paused: authoritative store binding is unavailable")
            else:
                self._pump()
        self.journal.submit("accept", internal_id, payload, success=accepted,
                            failure=lambda error: self.task_failed.emit(internal_id, error))

    def request_cancel(self, internal_id, external_id=None):
        self.mutation_changed.emit()
        ticket = self._ticket()
        def accepted(row):
            if self._current(ticket) and row["cancellation"] == "ACKNOWLEDGED":
                self.deletion_success.emit(internal_id, row["external_id"] or "")
            self._pump()
        self.journal.submit("cancel", internal_id, external_id, success=accepted,
                            failure=lambda error: self.task_failed.emit(internal_id, error))

    def queue_delete(self, internal_id, external_id):
        self.request_cancel(internal_id, external_id)

    def _pump(self):
        if not self._running or not self.gateway.is_ready():
            return
        if not self._authorized or self._authorized_epoch != self.gateway.generation:
            if self._authorized:
                self.scope_invalidated.emit()
            self._authorized = False
            self._validate_scope()
            return
        if self._busy:
            return
        self._busy = True
        ticket, gateway = self._ticket(), freeze_gateway(self.gateway)
        self._busy_ticket = ticket
        self.journal.submit("claim", True, success=lambda row: self._claimed(row, ticket, gateway), failure=self._storage_error)

    def _claimed(self, row, ticket, gateway):
        if not row:
            self._busy = False
            self._busy_ticket = None
            return
        if not self._current(ticket):
            self._release(ticket)
            return
        if row["operation"] == "ADD" and row["request"] is None:
            try:
                client = gateway.get_client()
                if client is None:
                    raise RuntimeError("Session was superseded before dispatch")
                request = client._build_variables_to_add_delivery(Order.model_validate(json.loads(row["payload"]))).model_dump(mode="json", by_alias=True)
                self.journal.submit("freeze_request", row["internal_id"], request,
                    success=lambda frozen: self._send(dict(frozen, operation="ADD"), ticket, gateway), failure=self._storage_error)
            except Exception as error:
                self._failed(row, ticket, {"message": str(error), "retryable": False})
        else:
            self._send(row, ticket, gateway)

    def _send(self, row, ticket, gateway):
        if not self._current(ticket) or not gateway.is_ready():
            self._release(ticket)
            return
        internal = row["internal_id"]
        self.mutation_changed.emit()
        worker = NativeApiWorker(gateway, "submit_import" if row["operation"] == "ADD" else "cancel_order",
                                 json.loads(row["request"]) if row["operation"] == "ADD" else internal,
                                 preflight=lambda: self._current(ticket) and self.journal.journal.dispatch_is_current(internal,row["attempts"],row["operation"]))
        if row["operation"] == "ADD":
            worker.signals.result.connect(lambda result: self.journal.submit("accepted_job", internal, result["jobId"], row["attempts"],
                success=lambda accepted: self._completed(ticket, accepted, self.import_accepted, internal, result["jobId"]), failure=self._storage_error))
        else:
            worker.signals.result.connect(lambda _: self.journal.submit("cancelled", internal,
                success=lambda accepted: self._completed(ticket, True, self.deletion_success, internal, row["external_id"] or ""), failure=self._storage_error))
        worker.signals.failed.connect(lambda error: self._failed(row, ticket, error))
        QThreadPool.globalInstance().start(worker)

    def _completed(self, ticket, accepted, signal, internal, external):
        if accepted and self._current(ticket):
            self.mutation_changed.emit()
            signal.emit(internal, external)
        self._release(ticket)

    def _failed(self, row, ticket, error):
        method = "failed" if error["retryable"] else "needs_review"
        def committed(accepted):
            if accepted and self._current(ticket):
                self.mutation_changed.emit()
                self.task_failed.emit(row["internal_id"], error["message"])
            self._release(ticket)
        self.journal.submit(method, row["internal_id"], row["operation"], error["message"], row["attempts"],
                            success=committed, failure=self._storage_error)

    def _release(self, ticket):
        if self._busy_ticket == ticket:
            self._busy = False
            self._busy_ticket = None
        self._pump()

    def _storage_error(self, error):
        self.stop()
        self._busy = False
        self.task_failed.emit("", "Sincronização pausada: " + error)
