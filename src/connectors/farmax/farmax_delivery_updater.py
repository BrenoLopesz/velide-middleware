import json
import logging
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, QTimer

from connectors.farmax.farmax_worker import FarmaxWorker
from connectors.farmax.identity import normalize_farmax_id


class FarmaxDeliveryUpdater(QObject):
    """Recognize persisted delivery fields. Never write ERP business state."""
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
        self._timezone = source_timezone
        self._busy = False
        self._running = False
        self._generation = 0
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._pump)

    def start(self):
        self._generation += 1
        self._running = True
        self._timer.start()
        self._pump()

    def stop(self):
        self._generation += 1
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
            self._on_error(internal_id, operation, "Durable authoritative observation is required")
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
        ticket = self._generation
        try:
            request = _ObservationRequest(self._journal.journal, self._repository, effect, self._timezone,
                                          lambda: self._running and ticket == self._generation)
            worker = FarmaxWorker(request, "observe")
            worker.signals.success.connect(lambda match: self._result(effect, ticket, observation=match))
            worker.signals.error.connect(lambda error: self._result(effect, ticket, error=error))
            self._thread_pool.start(worker)
        except Exception as error:
            self._result(effect, ticket, error=str(error))

    def _result(self, effect, ticket, error=None, observation=None):
        if ticket != self._generation or not self._running or (error is None and observation is None):
            self._busy = False
            return
        def acknowledged(accepted):
            self._busy = False
            if not accepted or ticket != self._generation or not self._running:
                return
            if error:
                self._on_error(effect["internal_id"], effect["operation"], error)
            else:
                self.update_success.emit(effect["internal_id"], "DONE" if observation == "COMPLETED_FIELDS_MATCH" else effect["operation"])
            self._pump()
        if self._journal and effect["sequence"] is not None:
            self._journal.submit("effect_result", effect["sequence"], error, effect["generation"], observation,
                                 success=acknowledged, failure=self._storage_error)
        else:
            acknowledged(False)

    def _storage_error(self, error):
        self.stop()
        self._busy = False
        self._on_error("", "PERSISTENCE", error)

    def _on_error(self, internal_id, operation, error):
        self._logger.error("Farmax %s %s: %s", internal_id, operation, error)
        self.update_failed.emit(internal_id, operation, error)


class _ObservationRequest:
    def __init__(self, journal, repository, effect, timezone, current):
        self.journal, self.repository, self.effect = journal, repository, effect
        self.timezone, self.current = timezone, current

    def observe(self):
        if not self.current():
            return None
        effect = self.journal.prepare_effect(self.effect["sequence"], self.effect["generation"])
        if not effect or not self.current():
            return None
        return self.repository.observe_delivery_effect(int(effect["internal_id"]), json.loads(effect["payload"]), self.timezone)
