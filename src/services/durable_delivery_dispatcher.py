import json

from PyQt5.QtCore import QObject, QTimer, QThreadPool, pyqtSignal
from api.velide_gateway import freeze_gateway
from models.velide_delivery_models import Order
from models.delivery_table_model import sync_row_status
from workers.native_api_worker import NativeApiWorker
from workers.legacy_sync_worker import LegacySyncWorker


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
        self._validation_ticket = None
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
        self._validation_ticket = ticket
        if not self.native:
            # Legacy uses its installed endpoint/source and current credentials.
            # Local journal restoration verifies the installed source binding;
            # no undeployed store-ID endpoint is required.
            self._validated(ticket)
            return
        worker = NativeApiWorker(freeze_gateway(self.gateway), "validate_scope")
        worker.signals.result.connect(lambda _: self._validated(ticket))
        worker.signals.failed.connect(lambda error: self._validation_failed(ticket, error))
        QThreadPool.globalInstance().start(worker)

    def _validated(self, ticket):
        if not self._current(ticket):
            if self._validation_ticket == ticket:
                self._validating = False
            return
        self.journal.submit("restore", success=lambda rows: self._restored(rows, ticket),
                            failure=lambda error: self._storage_error(error, ticket))

    def _validation_failed(self, ticket, error):
        if not self._current(ticket):
            if self._validation_ticket == ticket:
                self._validating = False
            return
        self._validating = False
        self.task_failed.emit("", error["message"])
        if not error["retryable"]:
            self.stop()

    def _restored(self, rows, ticket):
        if not self._current(ticket):
            if self._validation_ticket == ticket:
                self._validating = False
            return
        self._validating = False
        self._authorized = True
        self._authorized_epoch = ticket[1]
        self.scope_ready.emit()
        for row in rows:
            if row["payload"] and ((row["submission"] != "TERMINAL" and row["cancellation"] != "ACKNOWLEDGED") or row.get("last_error") or row.get("source_review")):
                order = Order.model_validate(json.loads(row["payload"]))
                order.ui_status_hint = sync_row_status(row)
                self.order_restored.emit(order, row["external_id"])
        self._pump()

    def queue_add(self, internal_id, order):
        payload = order.model_dump(mode="json", by_alias=True)
        payload["internal_id"] = internal_id
        args = (internal_id, payload)
        source_file = getattr(order, "_cds_source_file", None)
        if source_file is not None:
            args += (source_file,)
        self.journal.submit("accept", *args, success=lambda _: self._pump(),
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
        self.journal.submit("claim", bool(self.native), success=lambda row: self._claimed(row, ticket, gateway),
                            failure=lambda error: self._storage_error(error, ticket))

    def _claimed(self, row, ticket, gateway):
        if not row:
            if self._busy_ticket == ticket:
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
                    self._release(ticket)
                    return
                request = client._build_variables_to_add_delivery(Order.model_validate(json.loads(row["payload"]))).model_dump(mode="json", by_alias=True)
                if not self.native:
                    # Durable replay must have a stable server key even when the
                    # old no-journal caller opted out of idempotency.
                    request["idempotencyKey"] = row["internal_id"]
                self.journal.submit("freeze_request", row["internal_id"], request,
                    success=lambda frozen: self._send(dict(frozen, operation="ADD"), ticket, gateway),
                    failure=lambda error: self._storage_error(error, ticket))
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
        preflight = lambda: self._current(ticket) and self.journal.journal.dispatch_is_current(internal, row["attempts"], row["operation"])
        if not self.native:
            self._send_legacy(row, ticket, gateway, preflight)
            return
        worker = NativeApiWorker(gateway, "submit_import" if row["operation"] == "ADD" else "cancel_order",
                                  json.loads(row["request"]) if row["operation"] == "ADD" else internal,
                                  preflight=preflight)
        if row["operation"] == "ADD":
            worker.signals.result.connect(lambda result: self.journal.submit("accepted_job", internal, result["jobId"], row["attempts"],
                success=lambda accepted: self._completed(ticket, accepted, self.import_accepted, internal, result["jobId"]), failure=self._storage_error))
        else:
            worker.signals.result.connect(lambda _: self.journal.submit("cancelled", internal,
                success=lambda accepted: self._completed(ticket, accepted is not False, self.deletion_success, internal, row["external_id"] or ""), failure=self._storage_error))
        worker.signals.failed.connect(lambda error: self._failed(row, ticket, error, gateway.config))
        QThreadPool.globalInstance().start(worker)

    def _send_legacy(self, row, ticket, gateway, preflight):
        internal = row["internal_id"]
        try:
            if row["operation"] == "ADD":
                request = json.loads(row["request"])
                if not request.get("idempotencyKey"):
                    raise ValueError("O pedido salvo não tem chave de envio para recuperar uma resposta incerta. Confira a entrega no Velide antes de reenviar.")
                argument = Order.model_validate(json.loads(row["payload"]))
                argument.request_variables = request
            else:
                argument = row["external_id"]
                if not argument:
                    raise ValueError("O cancelamento ainda precisa recuperar a identificação da entrega no Velide.")
            worker = LegacySyncWorker(gateway, row["operation"], argument, preflight=preflight)
        except (ValueError, TypeError) as error:
            self._failed(row, ticket, {"message": str(error), "retryable": False}, gateway.config)
            return
        if row["operation"] == "ADD":
            worker.signals.result.connect(lambda external: self.journal.submit("imported", internal, external,
                success=lambda accepted: self._completed(ticket, bool(accepted), self.delivery_success, internal, external),
                failure=lambda error: self._storage_error(error, ticket)))
        else:
            worker.signals.result.connect(lambda external: self.journal.submit("cancelled", internal,
                success=lambda accepted: self._completed(ticket, accepted is not False, self.deletion_success, internal, external),
                failure=lambda error: self._storage_error(error, ticket)))
        worker.signals.failed.connect(lambda error: self._failed(row, ticket, error, gateway.config))
        QThreadPool.globalInstance().start(worker)

    def _completed(self, ticket, accepted, signal, internal, external):
        if accepted and self._current(ticket):
            self.mutation_changed.emit()
            signal.emit(internal, external)
        self._release(ticket)

    def _failed(self, row, ticket, error, config=None):
        if error.get("code") == "CANCELLATION_DEFERRED" and row["operation"] == "DELETE":
            def deferred(accepted):
                if accepted and self._current(ticket):
                    self.task_failed.emit(row["internal_id"], "Pedido cancelado no Farmax. Aguardando a entrega sair da rota ativa para cancelar no Velide.")
                self._release(ticket)
            self.journal.submit("defer_cancellation", row["internal_id"], row["attempts"], success=deferred,
                                failure=lambda message: self._storage_error(message, ticket))
            return
        if error.get("code") == "LOCAL_PRECONDITION":
            self._release(ticket)
            return  # The journal already has the newer cancellation/source state.
        method = "failed" if error["retryable"] else "needs_review"
        def committed(accepted):
            if accepted and self._current(ticket):
                self.mutation_changed.emit()
                self.task_failed.emit(row["internal_id"], error["message"])
            self._release(ticket)
        args = (row["internal_id"], row["operation"], error["message"], row["attempts"])
        if error["retryable"]:
            config = config or self.gateway.config
            args += (max(1, config.add_delivery_max_retries), config.add_delivery_initial_delay, config.add_delivery_backoff_factor)
        self.journal.submit(method, *args, success=committed, failure=lambda message: self._storage_error(message, ticket))

    def _release(self, ticket):
        if self._busy_ticket == ticket:
            self._busy = False
            self._busy_ticket = None
        self._pump()

    def _storage_error(self, error, ticket=None):
        if ticket is not None and not self._current(ticket):
            self._release(ticket)
            return
        self.stop()
        self._busy = False
        self.task_failed.emit("", "Sincronização pausada: " + error)
