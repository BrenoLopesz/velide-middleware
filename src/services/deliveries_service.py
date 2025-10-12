import collections
import logging
from typing import Dict
import uuid
from PyQt5.QtCore import QObject, pyqtSignal, Qt

from config import ApiConfig, TargetSystem
from api.velide import Velide
from models.delivery_table_model import DeliveryRowStatus
from services.strategies.deliveries_source_strategy import IDeliverySourceStrategy
from models.velide_delivery_models import Order
from utils.run_in_thread import run_in_thread
from workers.velide_worker import VelideWorker

class DeliveriesService(QObject):
    delivery_acknowledged = pyqtSignal(str, Order)
    delivery_update = pyqtSignal(str, DeliveryRowStatus)
    delivery_failed = pyqtSignal(str, str) # Order ID, error message

    def __init__(self, api_config: ApiConfig, target_system: TargetSystem):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._api_config = api_config
        self._target_system = target_system
        self._active_strategy: IDeliverySourceStrategy | None = None
        self._active_deliveries: Dict[str, Order] = {}
        self._velide_api: Velide | None = None
        self._delivery_queue = collections.deque()
        self._is_processing = False
    
    def set_access_token(self, access_token: str):
        self._velide_api = Velide(access_token, self._api_config, self._target_system) # Only CDS uses this class

    def set_strategy(self, strategy: IDeliverySourceStrategy):
        """Sets the active delivery source strategy."""
        if self._active_strategy:
            self._active_strategy.stop_listening() # Stop the old one

        self._active_strategy = strategy
        # Connect to the strategy's signals
        self._active_strategy.order_normalized.connect(self._on_order_normalized)

    def get_order(self, order_id: str) -> Order:
        return self._active_deliveries.get(order_id, None)

    def start_listening(self):
        if self._active_strategy is None:
            self.logger.critical("Não há nenhum software conectado para realizar a integração! Nenhuma entrega será reconhecida.")
            return 
        
        self._active_strategy.start_listening()

    def _on_delivery_success(self, order_id: str, api_response: dict):
        self.delivery_update.emit(order_id, DeliveryRowStatus.ADDED)
        self._active_deliveries.pop(order_id, None)

    def _on_delivery_failure(self, order_id: str, error_msg: str):
        self.delivery_failed.emit(order_id, error_msg)
        self.delivery_update.emit(order_id, DeliveryRowStatus.ERROR)
        self._active_deliveries.pop(order_id, None)

    def _send_delivery_to_api(self, order_id: str, order: Order):
        """
        Public method to request a delivery. Adds the order to a queue
        and triggers the processing loop.
        """
        self._delivery_queue.append((order_id, order))
        self._try_process_next() # Changed: Call the central dispatcher

    @run_in_thread("velide_api_call")
    def _process_delivery_queue(self):
        """
        MODIFIED: This method now only processes ONE item. It no longer
        triggers the next item in the chain. The manager logic does that.
        """
        # Safety check, though the dispatcher should prevent this.
        if not self._delivery_queue:
            return None

        order_id, order = self._delivery_queue.popleft()
        worker = VelideWorker(self._velide_api, order)

        worker.delivery_added.connect(
            lambda resp, oid=order_id: self._on_delivery_success(oid, resp)
        )
        worker.error.connect(
            lambda err, oid=order_id: self._on_delivery_failure(oid, err)
        )

        return worker
    
    def _try_process_next(self):
        """
        NEW: Central dispatcher.
        Checks if a task can be run and, if so, starts it and connects
        to its completion signal.
        """
        # If we are already processing a task or the queue is empty, do nothing.
        if self._is_processing or not self._delivery_queue:
            return

        self._is_processing = True
        
        # This call starts the decorated function. The decorator will create
        # and attach the thread and worker to `self`.
        self._process_delivery_queue()

        # KEY INSIGHT: The decorator has now run and set the thread attribute.
        # We can now access it and connect to the CORRECT signal.
        thread_attr = "_velide_api_call_thread"
        if hasattr(self, thread_attr):
            thread = getattr(self, thread_attr)
            # This is the safe way to chain tasks. We wait for the thread,
            # not just the worker, to be finished.
            thread.finished.connect(self._on_process_finished)
        else:
            # This is a safeguard. If the decorated function returned None
            # (e.g., queue was empty), the decorator creates no thread.
            self.logger.warning("Decorator did not create a thread. Resetting state.")
            self._is_processing = False

    def _on_process_finished(self):
        """
        NEW: This slot is only called after the QThread has completely finished
        and its cleanup (from the decorator) is about to run.
        """
        self._is_processing = False
        # Now that we're officially done, check if there's more work.
        self._try_process_next()

    def _on_order_normalized(self, normalized_order: Order):
        """This is the entry point for the GENERIC workflow."""
        order_id = str(uuid.uuid4())
        self._active_deliveries[order_id] = normalized_order

        # 1. Acknowledge (to the UI)
        self.delivery_acknowledged.emit(order_id, normalized_order)

        if self._velide_api is None:
            self.logger.error("Não é possível enviar entrega antes de efetuar autenticação! Aguardando...")
            self.delivery_update.emit(order_id, DeliveryRowStatus.ERROR) # May create a custom status later
            return

        # 2. Send to API
        try:
            self._send_delivery_to_api(order_id, normalized_order)
            # On success, update the status to show it's sent.
            self.delivery_update.emit(order_id, DeliveryRowStatus.SENDING) 
        except Exception as e:
            # If the API call fails, the order *already exists*.
            # We must now log the error AND update its status to ERROR.
            self.logger.exception(f"Falha inesperada ao enviar entrega para o Velide ({order_id}).")
            self.delivery_update.emit(order_id, DeliveryRowStatus.ERROR)

