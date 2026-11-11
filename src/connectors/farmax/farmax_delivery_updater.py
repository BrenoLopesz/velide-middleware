import json
import logging
from datetime import datetime

import pytz
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, QTimer

from connectors.farmax.farmax_worker import FarmaxWorker
from connectors.farmax.identity import normalize_farmax_id


class FarmaxDeliveryUpdater(QObject):
    """At-least-once, ordered Farmax effects acknowledged after Firebird commit."""
    update_success = pyqtSignal(str, str)
    update_failed = pyqtSignal(str, str, str)

    def __init__(self, repository, deliverymen_retriever, journal=None,
                 source_timezone="America/Sao_Paulo"):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._repository = repository
        self._thread_pool = QThreadPool.globalInstance()
        self._deliverymen_retriever = deliverymen_retriever
        self._journal = journal
        self._timezone = pytz.timezone(source_timezone)
        self._busy = False
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._pump)

    def start(self):
        self._running = True
        self._timer.start()
        self._pump()

    def stop(self):
        self._running = False
        self._timer.stop()

    def mark_as_in_route(self, order, deliveryman_external_id):
        self._enqueue(order, "IN_ROUTE", deliveryman_external_id)

    def mark_as_done(self, order):
        self._enqueue(order, "DONE", None)

    def _enqueue(self, order, operation, courier):
        payload = dict(order.effect_context or {})
        if not payload.get("occurred_at") or not payload.get("execution_id"):
            self._on_error(order.internal_id, operation, "Horário ou execução remota ausente; reconciliação necessária.")
            return
        payload["courier"] = courier
        internal_id = normalize_farmax_id(order.internal_id)
        key = payload["execution_id"] + ":" + operation
        if not self._journal:
            self._execute({"internal_id": internal_id, "operation": operation,
                           "payload": json.dumps(payload), "sequence": None})
            return
        self._journal.submit("enqueue_effect", internal_id, key, operation, payload, payload,
                             success=lambda _: self._pump(),
                             failure=lambda error: self._on_error(internal_id, operation, error))

    def _pump(self):
        if self._busy or not self._running or not self._journal:
            return
        self._busy = True
        self._journal.submit("next_effect", success=self._next, failure=self._storage_error)

    def _next(self, effect):
        if not effect or not self._running:
            self._busy = False
            return
        self._execute(effect)

    def _execute(self, effect):
        internal_id, operation = effect["internal_id"], effect["operation"]
        try:
            payload = json.loads(effect["payload"])
            occurred = datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00"))
            if occurred.tzinfo is None:
                raise ValueError("Horário remoto precisa incluir fuso horário")
            local_time = occurred.astimezone(self._timezone).time()
            if operation == "IN_ROUTE":
                driver = self._deliverymen_retriever.get_deliveryman_by_external_id(payload["courier"])
                driver_id = driver.id if driver else self._deliverymen_retriever.get_mapped_local_id(payload["courier"])
                if not isinstance(driver_id, (str, int, float)):
                    raise ValueError("Entregador não foi encontrado")
                worker = FarmaxWorker.for_update_delivery_as_in_route(
                    self._repository, sale_id=int(internal_id),
                    driver_id=int(normalize_farmax_id(driver_id)), left_at=local_time, historical=True)
            else:
                worker = FarmaxWorker.for_update_delivery_as_done(
                    self._repository, sale_id=int(internal_id), ended_at=local_time)
            worker.signals.success.connect(lambda _: self._result(effect))
            worker.signals.error.connect(lambda error: self._result(effect, error))
            self._thread_pool.start(worker)
        except Exception as error:
            self._result(effect, str(error))

    def _result(self, effect, error=None):
        def acknowledged(_=None):
            self._busy = False
            if error:
                self._on_error(effect["internal_id"], effect["operation"], error)
            else:
                self.update_success.emit(effect["internal_id"], effect["operation"])
            self._pump()
        if self._journal and effect["sequence"] is not None:
            self._journal.submit("effect_result", effect["sequence"], error,
                                 success=acknowledged, failure=self._storage_error)
        else:
            acknowledged()

    def _storage_error(self, error):
        self.stop()
        self._busy = False
        self._on_error("", "PERSISTENCE", error)

    def _on_error(self, internal_id, operation, error):
        self._logger.error("Farmax %s %s: %s", internal_id, operation, error)
        self.update_failed.emit(internal_id, operation, error)
