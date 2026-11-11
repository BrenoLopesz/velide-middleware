import json

from PyQt5.QtCore import QObject, QTimer, QThreadPool, pyqtSignal

from models.velide_delivery_models import Order
from workers.velide_worker import VelideWorker
from workers.native_api_worker import NativeApiWorker


class DurableDeliveryDispatcher(QObject):
    delivery_success = pyqtSignal(str, str)
    deletion_success = pyqtSignal(str, str)
    task_failed = pyqtSignal(str, str)
    order_restored = pyqtSignal(Order, object)
    import_accepted = pyqtSignal(str, str)

    def __init__(self, gateway, journal):
        super().__init__()
        self.gateway, self.journal = gateway, journal
        self.native = getattr(gateway.config, "native", None)
        self._running = False
        self._authorized = not bool(self.native)
        self._generation = 0
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._pump)

    def start(self):
        self._generation += 1
        self._authorized = not bool(self.native)
        self._running = True
        if self.native:
            self._validate_scope()
        else:
            self._load()

    def _validate_scope(self):
        if not self._running:
            return
        generation = self._generation
        worker = NativeApiWorker(self.gateway, "validate_scope")
        worker.signals.result.connect(lambda _: self._validated(generation))
        worker.signals.failed.connect(lambda error: self._validation_failed(error) if generation == self._generation else None)
        QThreadPool.globalInstance().start(worker)

    def _validated(self, generation):
        if generation == self._generation and self._running:
            self._authorized = True
            self._load()

    def _validation_failed(self, error):
        if error["retryable"]:
            self.task_failed.emit("", error["message"])
            QTimer.singleShot(5000, self._validate_scope)
        else:
            self._storage_error(error["message"])

    def _load(self):
        if not self._running:
            return
        self.journal.submit("restore", success=self._restored, failure=self._storage_error)

    def stop(self):
        self._generation += 1
        self._authorized = False
        self._running = False
        self._timer.stop()

    def _restored(self, rows):
        if not self._running:
            return
        for row in rows:
            if row["payload"] and row["submission"] != "TERMINAL" and row["cancellation"] != "ACKNOWLEDGED":
                order = Order.model_validate(json.loads(row["payload"]))
                self.order_restored.emit(order, row["external_id"])
        self._timer.start()
        self._pump()

    def queue_add(self, internal_id, order):
        payload = order.model_dump(mode="json", by_alias=True)
        payload["internal_id"] = internal_id
        self.journal.submit("accept", internal_id, payload,
                            success=lambda _: self._pump(),
                            failure=lambda error: self.task_failed.emit(internal_id, error))

    def request_cancel(self, internal_id, external_id=None):
        def accepted(row):
            if row["cancellation"] == "ACKNOWLEDGED":
                self.deletion_success.emit(internal_id, row["external_id"] or "")
            else:
                self._pump()
        self.journal.submit("cancel", internal_id, external_id, success=accepted,
                            failure=lambda error: self.task_failed.emit(internal_id, error))

    def queue_delete(self, internal_id, external_id):
        self.request_cancel(internal_id, external_id)

    def _pump(self):
        if not self._running or not self._authorized or self._busy or not self.gateway.is_ready():
            return
        self._busy = True
        self.journal.submit("claim", bool(self.native), success=self._claimed, failure=self._storage_error)

    def _claimed(self, row):
        if row is None or not self._running:
            self._busy = False
            return
        if row["operation"] == "ADD" and row["request"] is None:
            try:
                order = Order.model_validate(json.loads(row["payload"]))
                client = self.gateway.get_client()
                if client is None:
                    raise RuntimeError("API indisponível")
                request = client._build_variables_to_add_delivery(order).model_dump(mode="json", by_alias=True)
                # Durable recovery always uses the deployed idempotency capability.
                if not self.native:
                    request["idempotencyKey"] = order.internal_id
                self.journal.submit("freeze_request", row["internal_id"], request,
                                    success=lambda frozen: self._send(dict(frozen, operation="ADD")),
                                    failure=self._storage_error)
            except Exception as error:
                self._failed(row, str(error))
            return
        self._send(row)

    def _send(self, row):
        if not self._running or not self._authorized:
            self._busy = False
            return
        internal_id = row["internal_id"]
        if self.native:
            operation = "submit_import" if row["operation"] == "ADD" else "cancel_order"
            argument = json.loads(row["request"]) if row["operation"] == "ADD" else internal_id
            worker = NativeApiWorker(self.gateway, operation, argument)
            if row["operation"] == "ADD":
                worker.signals.result.connect(lambda result: self.journal.submit("accepted_job", internal_id, result["jobId"],
                    success=lambda _: self._completed(self.import_accepted, internal_id, result["jobId"]), failure=self._storage_error))
            else:
                worker.signals.result.connect(lambda _: self.journal.submit("cancelled", internal_id,
                    success=lambda _: self._completed(self.deletion_success, internal_id, row["external_id"] or ""), failure=self._storage_error))
            worker.signals.failed.connect(lambda error: self._native_failed(row, error))
            QThreadPool.globalInstance().start(worker)
            return
        if row["operation"] == "ADD":
            order = Order.model_validate(json.loads(row["payload"]))
            order.request_variables = json.loads(row["request"])
            worker = VelideWorker.for_add_delivery(self.gateway, order)
            worker.signals.delivery_added.connect(lambda response: self._added(row, response))
        else:
            worker = VelideWorker.for_delete_delivery(self.gateway, row["external_id"])
            worker.signals.delivery_deleted.connect(
                lambda external: self.journal.submit("cancelled", internal_id,
                    success=lambda _: self._completed(self.deletion_success, internal_id, external),
                    failure=self._storage_error))
        worker.signals.error.connect(lambda error: self._failed(row, error))
        QThreadPool.globalInstance().start(worker)

    def _added(self, row, response):
        external = response.get("id")
        if not external:
            self._failed(row, "API returned no delivery ID")
            return
        self.journal.submit("imported", row["internal_id"], external,
                            success=lambda _: self._completed(self.delivery_success, row["internal_id"], external),
                            failure=self._storage_error)

    def _completed(self, signal, internal_id, external_id):
        if self._running:
            signal.emit(internal_id, external_id)
        self._busy = False
        self._pump()

    def _failed(self, row, error):
        self.journal.submit("failed", row["internal_id"], row["operation"], error,
                            success=lambda _: self._failure_recorded(row["internal_id"], error),
                            failure=self._storage_error)

    def _failure_recorded(self, internal_id, error):
        self.task_failed.emit(internal_id, error)
        self._busy = False
        self._pump()

    def _native_failed(self, row, error):
        if error["retryable"]:
            self._failed(row, error["message"])
        else:
            self.journal.submit("needs_review", row["internal_id"], row["operation"], error["message"],
                                success=lambda _: self._failure_recorded(row["internal_id"], error["message"]),
                                failure=self._storage_error)

    def _storage_error(self, error):
        # Do not run another network write when its local acknowledgement failed.
        self._running = False
        self._timer.stop()
        self._busy = False
        self.task_failed.emit("", "Sincronização pausada: " + error)
