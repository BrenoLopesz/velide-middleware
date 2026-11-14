import logging
import json
import os
from typing import Optional
from utils.source_identity import cds_file_identity
from utils.log_messages import humanize_error
from PyQt5.QtCore import QThreadPool
from pydantic import ValidationError
from models.velide_delivery_models import Order
from models.delivery_table_model import sync_row_status
from models.cds_order_model import CdsOrder
from services.strategies.connectable_strategy import IConnectableStrategy
from config import ApiConfig
from workers.cds_logs_listener_worker import CdsLogsListenerWorker


class CdsStrategy(IConnectableStrategy):
    """Observe CDS receipts and publish held rows through the restoration signal.

    ``pending_files`` permits file-specific observation past the legacy baseline.
    The installer may include unresolved inventory files when journal.accept
    enforces cds_upgrade_hold; observing a file does not authorize its dispatch.
    """
    def __init__(self, api_config: ApiConfig, folder_to_watch: str, journal=None, baseline_path=None, pending_files=()):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._api_config = api_config
        self._folder_to_watch = folder_to_watch
        self._journal = journal
        # Tie the observation boundary to the installed database, not a release
        # directory. The explicit hook also supports an installer-created baseline.
        journal_path = getattr(getattr(journal, "journal", None), "path", None)
        if baseline_path is None and isinstance(journal_path, (str, os.PathLike)):
            baseline_path = os.fspath(journal_path) + ".cds-observation.json"
        self._baseline_path = baseline_path
        self._pending_files = tuple(pending_files)
        self._running = False
        self._announced = set()
        self._local_orders = {}
        self._invalid_files = {}
        self._reported_holds = {}

        # These workers will be created and moved to threads later
        self._file_listener_worker = None
        self._thread_pool = QThreadPool.globalInstance()

    def start_listening(self):
        """Creates and starts the file listener worker in a background thread."""
        if self._running:
            return
        self._running = True
        self._file_listener_worker = CdsLogsListenerWorker(self._folder_to_watch, self._baseline_path, self._pending_files)
        worker = self._file_listener_worker
        worker.signals.new_order.connect(lambda data: self._on_new_file_found(data, worker))
        worker.signals.error.connect(lambda error: self.synchronization_failed.emit("", error) if worker is self._file_listener_worker and self._running else None)
        worker.signals.finished.connect(lambda: self._listener_finished(worker))
        self._thread_pool.start(self._file_listener_worker)

    def _listener_finished(self, worker):
        if worker is self._file_listener_worker:
            self._running = False

    def stop_listening(self):
        self._running = False
        if self._file_listener_worker is None:
            return
        self._file_listener_worker.stop()

    def fetch_deliverymen(self, success, error):
        raise NotImplementedError

    def on_delivery_added(self, internal_id: str, external_id: str):
        return  # Just ignore it.

    def on_delivery_failed(self, internal_id: Optional[float]):
        return  # Just ignore it.

    def _on_new_file_found(self, delivery_data: dict, origin=None):
        if not self._running or (origin is not None and origin is not self._file_listener_worker):
            return
        try:
            cds_order: CdsOrder = CdsOrder.model_validate(delivery_data)
            normalized_order = self._transform(cds_order, delivery_data["_source_file"])
            # Runtime provenance survives the strategy -> service -> dispatcher
            # signal chain without changing the immutable business payload.
            normalized_order._cds_source_file = delivery_data["_source_file"]
            self._invalid_files.pop(delivery_data["_source_file"], None)
            observation = delivery_data.get("_source_observation")
            payload = normalized_order.model_dump(mode="json", by_alias=True)
            payload["internal_id"] = normalized_order.internal_id
            if self._journal:
                self._journal.submit("accept", normalized_order.internal_id, payload, delivery_data["_source_file"],
                    success=lambda row: self._accepted(normalized_order, row, observation, origin),
                    failure=lambda error: self._accept_failed(normalized_order, error, observation, origin))
            else:
                previous = self._local_orders.get(normalized_order.internal_id)
                if previous is not None and previous != payload:
                    message = "Os dados deste pedido CDS foram alterados após a leitura. Confira o pedido antes de reenviar."
                    self.logger.error(message)
                    self.synchronization_failed.emit(normalized_order.internal_id, message)
                    return
                if previous is None:
                    self._local_orders[normalized_order.internal_id] = payload
                    self.order_normalized.emit(normalized_order)
                self._acknowledge_file(observation)
        except ValidationError as error:
            filename = delivery_data.get("_source_file", "recebido")
            observation = delivery_data.get("_source_observation", ())
            signature = (tuple(observation[1:3]), str(error))
            if self._invalid_files.get(filename) != signature:
                self._invalid_files[filename] = signature
                self.logger.exception(
                    "O arquivo CDS %s contém dados incompletos ou inválidos. "
                    "Confira os dados do pedido no CDS; a leitura será tentada novamente.", filename)
            return
        except Exception:
            self.logger.exception(
                "Não foi possível preparar o pedido CDS. A leitura será tentada novamente. "
                "Se o problema continuar, consulte o suporte com os registros de diagnóstico."
            )
            return

    def _acknowledge_file(self, observation):
        if observation and self._file_listener_worker:
            self._file_listener_worker.acknowledge(observation)

    def _accepted(self, order, row, observation=None, origin=None):
        if not self._running or (origin is not None and origin is not self._file_listener_worker):
            return
        self._acknowledge_file(observation)
        if row.get("source_review") or row["submission"] in ("NEEDS_REVIEW", "UNKNOWN"):
            self._publish_held(order, row, origin)
            return
        if row["submission"] != "TERMINAL" and row["cancellation"] != "ACKNOWLEDGED":
            if order.internal_id not in self._announced:
                self._announced.add(order.internal_id)
                self.logger.info("Pedido CDS de %s recebido e salvo para sincronização.", order.customer_name)
            self.order_normalized.emit(order)

    def _accept_failed(self, order, error, observation=None, origin=None):
        if not self._running or (origin is not None and origin is not self._file_listener_worker):
            return
        # Only the journal's explicit post-commit conflict can be acknowledged.
        # Disk/validation failures remain retryable from the unchanged source file.
        if "durable review hold recorded" in str(error):
            self._acknowledge_file(observation)
            self._journal.submit("get_order", order.internal_id,
                success=lambda row: self._publish_held(order, row, origin),
                failure=lambda failure: self.synchronization_failed.emit(order.internal_id, humanize_error(failure))
                    if self._running and (origin is None or origin is self._file_listener_worker) else None)
            return
        self.synchronization_failed.emit(order.internal_id, humanize_error(error))

    def _publish_held(self, observed_order, row, origin=None):
        if not self._running or (origin is not None and origin is not self._file_listener_worker) or not row:
            return
        # Display the saved receipt, not an edited/reprinted replacement of it.
        order = Order.model_validate(json.loads(row["payload"])) if row.get("payload") else observed_order
        order.ui_status_hint = sync_row_status(row)
        self.order_restored.emit(order, row.get("external_id"))
        review = row.get("source_review") or {}
        if isinstance(review, str):
            review = json.loads(review)
        error = row.get("last_error") or review.get("message") or review.get("kind") or "Source review is required"
        if self._reported_holds.get(order.internal_id) != error:
            self._reported_holds[order.internal_id] = error
            self.synchronization_failed.emit(order.internal_id, humanize_error(error))

    def _transform(self, cds_order: CdsOrder, source_file: str) -> Order:
        """Transforms a source-specific CdsOrder into the common Order model."""
        splitted_address = cds_order.endereco.split(" - ")
        formatted_address = splitted_address[0]
        neighbourhood = None
        if len(splitted_address) == 6:
            neighbourhood = splitted_address[2]

        order = Order(
            customerName=cds_order.nome_cliente,
            customerContact=cds_order.contato_cliente,
            address=formatted_address,
            createdAt=cds_order.horario_pedido,
            address2=cds_order.complemento,
            reference=cds_order.referencia,
            neighbourhood=neighbourhood,
            # Same filename + original receipt is a reprint; changed data with
            # that identity conflicts in the journal instead of creating a copy.
            internal_id=cds_file_identity(source_file, cds_order.horario_pedido),
            ui_status_hint=None,
        )
        if len(splitted_address) != 6:
            self.logger.warning(
                "O endereço do pedido CDS de %s está fora do formato esperado. "
                "Confira o endereço e o bairro no Velide.", cds_order.nome_cliente)
        return order
