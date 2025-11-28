import collections
import logging
from typing import Dict, Optional
import uuid
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from config import ApiConfig, TargetSystem
from api.velide import Velide
from models.delivery_table_model import DeliveryRowStatus
from services.strategies.connectable_strategy import IConnectableStrategy
from models.velide_delivery_models import Order
from workers.velide_worker import VelideWorker

TASK_ADD = "ADD"
TASK_DELETE = "DELETE"

class DeliveriesService(QObject):
    delivery_acknowledged = pyqtSignal(str, Order)
    delivery_update = pyqtSignal(str, DeliveryRowStatus)
    delivery_failed = pyqtSignal(str, str) # Order ID, error message

    def __init__(self, api_config: ApiConfig, target_system: TargetSystem):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._api_config = api_config
        self._target_system = target_system
        self._active_strategy: Optional[IConnectableStrategy] = None
        self._active_deliveries: Dict[str, Order] = {}
        self._velide_api: Optional[Velide] = None
        self._delivery_queue = collections.deque()
        self._is_processing = False
        self._thread_pool = QThreadPool.globalInstance()
    
    def set_access_token(self, access_token: str):
        self._velide_api = Velide(access_token, self._api_config, self._target_system)

    def set_strategy(self, strategy: IConnectableStrategy):
        """Sets the active delivery source strategy."""
        if self._active_strategy:
            self._active_strategy.stop_listening() # Stop the old one

        self._active_strategy = strategy
        # Connect to the strategy's signals
        self._active_strategy.order_normalized.connect(self._on_order_normalized)
        self._active_strategy.order_cancelled.connect(self._on_order_cancelled)
        self._active_strategy.order_restored.connect(self._on_order_restored)
    
    def _on_order_restored(self, restored_order: Order):
        """
        Handles orders that were recovered from persistence on startup.
        Updates UI and Memory, but DOES NOT send to API.
        """
        internal_id = restored_order.internal_id
        
        # 1. Update Internal Memory
        self._active_deliveries[internal_id] = restored_order
        
        # 2. Acknowledge to UI (Populates the table)
        self.delivery_acknowledged.emit(internal_id, restored_order)
        
        # TODO: Use real status
        # 3. Restore the correct Status icon in the UI
        # We need to assume the Order comes with a status, or we default to ADDED 
        # since it was restored from a persistence layer that tracks active/added orders.
        # You might want to map persistence status to DeliveryRowStatus here.
        self.delivery_update.emit(internal_id, DeliveryRowStatus.ADDED)
        
        self.logger.debug(f"Pedido {internal_id} restaurado na interface visual.")

    def get_order(self, order_id: str) -> Order:
        return self._active_deliveries.get(order_id, None)

    def start_listening(self):
        if self._active_strategy is None:
            self.logger.critical("Não há nenhum software conectado para realizar a integração! Nenhuma entrega será reconhecida.")
            return 
        
        self._active_strategy.start_listening()

    def _on_delivery_success(self, order_id: str, api_response: dict):
        """
        Handles success from Velide API.
        
        Args:
            order_id: The temporary UUID used by the UI/Service.
            api_response: The raw JSON dictionary returned by the GraphQL mutation.
        """
        # 1. Update UI Status immediately
        self.delivery_update.emit(order_id, DeliveryRowStatus.ADDED)
        
        # 2. Retrieve the original Order object to find the Internal ID
        order_obj = self._active_deliveries.get(order_id)
        
        # 3. Extract External ID (Velide ID) from the GraphQL structure
        # Structure based on your provided models:
        # { "data": { "addDeliveryFromIntegration": { "id": "..." } } }
        external_id = api_response.get("id")
        if external_id is None:
            self.logger.error(f"Formato de resposta da API inválido: {api_response}")
            return

        # 4. Perform the Handshake
        if order_obj and self._active_strategy:
            # Retrieve the hidden field we added in Step 1
            internal_id = getattr(order_obj, 'internal_id', None)
            
            if internal_id is not None and external_id:
                # SUCCESS: Link Farmax ID <-> Velide ID in persistence
                # This fixes "Part 2" - the re-add loop on restart
                if hasattr(self._active_strategy, 'on_delivery_added'):
                    self._active_strategy.on_delivery_added(internal_id, external_id)
            else:
                self.logger.warning(
                    f"Entrega enviada, mas IDs não puderam ser vinculados. "
                    f"Internal: {internal_id}, External: {external_id}"
                )

        # 5. Cleanup memory
        self._active_deliveries.pop(order_id, None)

    def _on_deletion_success(self, internal_id: str, deleted_external_id: str):
        """Callback when API confirms deletion."""
        self.logger.info(f"Entrega {internal_id} ({deleted_external_id}) cancelada no Velide com sucesso.")
        self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED)

    def _on_delivery_failure(self, order_id: str, error_msg: str):
        self.delivery_failed.emit(order_id, error_msg)
        self.delivery_update.emit(order_id, DeliveryRowStatus.ERROR)
        self._active_deliveries.pop(order_id, None)

    def _send_delivery_to_api(self, order_id: str, order: Order):
        """
        Public method to request a delivery. Adds the order to a queue
        and triggers the processing loop.
        """
        task = {
            'type': TASK_ADD,
            'id': order_id,
            'payload': order
        }
        self._delivery_queue.append(task)
        self._try_process_next()

    def _send_cancellation_to_api(self, internal_id: str, external_id: str):
        """Adds a delete task to the queue."""
        task = {
            'type': TASK_DELETE,
            'id': internal_id,   # Used for UI updates
            'ext_id': external_id, # Used for API call
            'payload': None
        }
        self._delivery_queue.append(task)
        self.delivery_update.emit(internal_id, DeliveryRowStatus.DELETING) # Optional: Add a DELETING status
        self._try_process_next()

    def _process_delivery_queue(self):
        """
        ROUTER: Decides which worker to create based on task type.
        """
        if not self._delivery_queue:
            return None

        # Peek at the task (don't pop yet if you want to be super safe, 
        # but popping is fine here since we have the lock)
        task = self._delivery_queue.popleft()
        
        task_type = task['type']
        internal_id = task['id']

        worker = None

        if task_type == TASK_ADD:
            order = task['payload']
            worker = VelideWorker.for_add_delivery(self._velide_api, order)
            
            # Connect Success for ADD
            worker.signals.delivery_added.connect(
                lambda resp, oid=internal_id: self._on_delivery_success(oid, resp)
            )

        elif task_type == TASK_DELETE:
            external_id = task['ext_id']
            worker = VelideWorker.for_delete_delivery(self._velide_api, external_id)
            
            # Connect Success for DELETE
            worker.signals.delivery_deleted.connect(
                lambda del_id, oid=internal_id: self._on_deletion_success(oid, del_id)
            )

        # Connect Common Signals (Error & Finished)
        worker.signals.error.connect(
            lambda err, oid=internal_id: self._on_delivery_failure(oid, err)
        )
        # Crucial for the loop to continue:
        worker.signals.finished.connect(self._on_process_finished)

        self._thread_pool.start(worker)
    
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
        internal_id = normalized_order.internal_id
        self._active_deliveries[internal_id] = normalized_order

        # TODO: "order_normalize" triggers both the UI and sending to Velide.
        #       It is needed to has some way to display the delivery on the UI
        #       (eg.: when the SQLite is hydrated), without adding to Velide.

        # 1. Acknowledge (to the UI)
        self.delivery_acknowledged.emit(internal_id, normalized_order)

        if self._velide_api is None:
            self.logger.error("Não é possível enviar entrega antes de efetuar autenticação! Aguardando...")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.ERROR) # May create a custom status later
            return

        # 2. Send to API
        try:
            self._send_delivery_to_api(internal_id, normalized_order)
            # On success, update the status to show it's sent.
            self.delivery_update.emit(internal_id, DeliveryRowStatus.SENDING) 
        except Exception as e:
            # If the API call fails, the order *already exists*.
            # We must now log the error AND update its status to ERROR.
            self.logger.exception(f"Falha inesperada ao enviar entrega para o Velide ({internal_id}).")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.ERROR)

    def _on_order_cancelled(self, internal_id: str, external_id: Optional[str]):
        """
        Handles the cancellation signal from the strategy.
        
        1. Checks if the order is currently waiting in the queue to be added. 
           If yes, we just remove it from the queue (Optimization: saves 1 API call).
        2. If it was already sent (has external_id), we queue a delete task.
        """
        self.logger.debug(f"Solicitação de cancelamento recebida. Internal: {internal_id}, External: {external_id}")

        # 1. OPTIMIZATION: Check if this order is currently in the queue waiting to be sent.
        # If we haven't sent it yet, we don't need to delete it on the API. 
        # We just cancel the "Add" job.
        found_in_queue = False
        
        # We iterate a copy to safely modify the original if needed
        for i, task in enumerate(list(self._delivery_queue)):
            # Check if it is an ADD task for this internal ID
            if task['type'] == TASK_ADD and task['id'] == internal_id:
                del self._delivery_queue[i]
                found_in_queue = True
                self.logger.info(f"Pedido {internal_id} removido da fila de envio antes do processamento.")
                
                # Update UI to show it was cancelled locally
                self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED) 
                break
        
        if found_in_queue:
            return

        # 2. If not in queue, it might be currently processing (in the thread) 
        #    or already sent. If we have an external_id, we must request deletion.
        if external_id:
            self._send_cancellation_to_api(internal_id, external_id)
        else:
            self.logger.warning(f"Pedido {internal_id} cancelado, mas não tinha ID externo e não estava na fila. Nada a fazer.")
            # Ensure UI reflects cancellation
            self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED)
