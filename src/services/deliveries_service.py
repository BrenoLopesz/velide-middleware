import logging
from typing import Optional
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from config import ApiConfig, TargetSystem
from api.velide import Velide
from models.delivery_table_model import DeliveryRowStatus
from repositories.deliveries_repository import DeliveryRepository
from services.deliveries_dispatcher import DeliveryDispatcher
from services.strategies.connectable_strategy import IConnectableStrategy
from models.velide_delivery_models import Order
from services.velide_action_handler import VelideActionHandler

class DeliveriesService(QObject):
    delivery_acknowledged = pyqtSignal(str, Order)
    delivery_update = pyqtSignal(str, DeliveryRowStatus)
    delivery_failed = pyqtSignal(str, str) # Order ID, error message

    def __init__(
            self, 
            api_config: ApiConfig, 
            target_system: TargetSystem, 
            delivery_repository: DeliveryRepository
        ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        
        # 1. DEPENDENCY INJECTION / COMPOSITION
        self._repository = delivery_repository
        self._dispatcher = DeliveryDispatcher(QThreadPool.globalInstance())
        
        # 2. CONFIGURATION
        self._api_config = api_config
        self._target_system = target_system
        self._active_strategy: Optional[IConnectableStrategy] = None
        self._velide_api: Optional[Velide] = None

        # 3. CONNECT INTERNAL DISPATCHER SIGNALS
        # This bridges the gap between the Worker -> Dispatcher -> Service
        self._dispatcher.delivery_success.connect(self._on_add_delivery_request_success)
        self._dispatcher.deletion_success.connect(self._on_deletion_request_success)
        self._dispatcher.task_failed.connect(self._on_delivery_request_failure)

    def set_access_token(self, access_token: str):
        self._velide_api = Velide(access_token, self._api_config, self._target_system)
        # Pass API to dispatcher so it can create workers
        self._dispatcher.set_api(self._velide_api)

    def get_order(self, order_id: str) -> Order:
        """Facade for the repository."""
        return self._repository.get_by_internal(order_id)

    # =========================================================================
    # STRATEGY CONNECTION (Input from ERP)
    # =========================================================================

    def set_strategy(self, strategy: IConnectableStrategy):
        """Sets the active delivery source strategy."""
        if self._active_strategy:
            self._active_strategy.stop_listening() # Stop the old one

        self._active_strategy = strategy
        
        # Connect Strategy Signals
        self._active_strategy.order_normalized.connect(self._on_order_normalized)
        self._active_strategy.order_cancelled.connect(self._on_internal_order_cancelled)
        self._active_strategy.order_restored.connect(self._on_order_restored)

    def start_listening(self):
        if self._active_strategy is None:
            self.logger.critical("Nenhum sistema conectado! Entregas não serão processadas.")
            return 
        self._active_strategy.start_listening()

    # =========================================================================
    # CORE EVENT HANDLERS (Refactored Logic)
    # =========================================================================

    def _on_order_restored(self, restored_order: Order, external_id: Optional[str]):
        """
        Handles orders recovered from persistence (Startup).
        Logic: Update Memory + Update UI. Do NOT send to API.
        """
        # Sanitize ID before anything else
        self._sanitize_order_id(restored_order)

        internal_id = restored_order.internal_id
        
        # 1. Update Repository
        self._repository.add(restored_order)
        # If the restored order has an external ID, ensure we link it immediately
        if external_id is not None:
            self._repository.link_ids(internal_id, external_id)
        
        # 2. Acknowledge to UI
        self.delivery_acknowledged.emit(internal_id, restored_order)
        
        # Use actual status
        order_status = getattr(restored_order, "ui_status_hint", DeliveryRowStatus.ADDED)

        self.delivery_update.emit(internal_id, order_status)
        
        self.logger.debug(f"Pedido {internal_id} restaurado na interface visual.")

    def _on_order_normalized(self, normalized_order: Order):
        """
        Handles NEW orders coming from the ERP.
        Logic: Update Memory + Update UI + Send to API (via Dispatcher).
        """
        # Sanitize ID before anything else
        self._sanitize_order_id(normalized_order)

        internal_id = normalized_order.internal_id
        
        # 1. Update Repository
        self._repository.add(normalized_order)

        # 2. Acknowledge to UI
        self.delivery_acknowledged.emit(internal_id, normalized_order)

        if self._velide_api is None:
            self.logger.error("Não é possível enviar entrega antes de efetuar autenticação! Aguardando...")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.ERROR) # May create a custom status later
            return

        # 3. Send to API (Replaces old _send_delivery_to_api)
        try:
            self._dispatcher.queue_add(internal_id, normalized_order)
            self.delivery_update.emit(internal_id, DeliveryRowStatus.SENDING)
        except Exception:
            self.logger.exception(f"Falha inesperada ao adicionar pedido {internal_id} à fila.")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.ERROR)

    def _on_internal_order_cancelled(self, internal_id: str, external_id: Optional[str]):
        """
        Handles cancellation requests.
        Logic: Check Queue (Optimization) -> OR -> Send Delete Task.
        """
        self.logger.debug(f"Solicitação de cancelamento recebida. Interno: {internal_id}, Externo: {external_id}")

        # 1. OPTIMIZATION: Check if it's pending in the Dispatcher
        # We delegate the queue check to the Dispatcher.
        was_pending = self._dispatcher.cancel_pending_add(internal_id)

        if was_pending:
            self.logger.info(f"Pedido {internal_id} removido da fila de envio antes do processamento.")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED)
            self._repository.remove(internal_id) # Cleanup memory
            return

        # 2. If not pending, we must delete via API if we have an External ID
        if external_id:
            # Replaces old _send_cancellation_to_api
            self._dispatcher.queue_delete(internal_id, external_id)
            self.delivery_update.emit(internal_id, DeliveryRowStatus.DELETING)
        else:
            self.logger.warning(f"Pedido {internal_id} cancelado localmente (nenhum ID externo encontrado).")
            self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED)
            self._repository.remove(internal_id)

    # =========================================================================
    # RECONCILIATION CALLBACKS
    # =========================================================================

    def on_reconciliation_detects_route_start(self, internal_id: str):
        # TODO: Handle missing order (is it needed?)
        order = self.get_order(internal_id)
        self.on_delivery_route_started_in_velide(order)

    # =========================================================================
    # DISPATCHER CALLBACKS (Results from API)
    # =========================================================================

    def _on_add_delivery_request_success(self, internal_id: str, external_id: str):
        """Called when Dispatcher successfully POSTs to Velide."""
        
        # 1. Link IDs in Repository (Crucial for Websockets!)
        self._repository.link_ids(internal_id, external_id)
        
        # 2. Notify Strategy (Persistence handshake)
        if self._active_strategy and hasattr(self._active_strategy, 'on_delivery_added'):
            self._active_strategy.on_delivery_added(internal_id, external_id)
        
        # 3. Update UI
        self.delivery_update.emit(internal_id, DeliveryRowStatus.ADDED)
        
        # 4. Cleanup Memory? 
        # CAREFUL: In your original code you popped it: self._active_deliveries.pop(order_id)
        # But if you remove it, WebSocket updates won't work!
        # Recommendation: Keep it in memory, or move it to a "Completed" list if needed.
        # self._repository.remove(internal_id) <--- REMOVED this compared to original to allow WS updates

    def _on_deletion_request_success(self, internal_id: str, external_id: str):
        """Called when Dispatcher successfully DELETEs from Velide."""
        self.logger.info(f"Entrega {internal_id} ({external_id}) cancelada no Velide com sucesso.")
        self.delivery_update.emit(internal_id, DeliveryRowStatus.CANCELLED)
        self._repository.remove(internal_id)

    def _on_delivery_request_failure(self, internal_id: str, error_msg: str):
        """Called when Dispatcher encounters an error."""
        self.delivery_failed.emit(internal_id, error_msg)
        self.delivery_update.emit(internal_id, DeliveryRowStatus.ERROR)
        # Original code removed it on failure. Depending on retry logic, you might want to keep it.
        # self._repository.remove(internal_id)

    def on_delivery_deleted_in_velide(self, order: Order):
        self.logger.debug("Solicitando strategy para lidar com a entrega deletada.")
        self._active_strategy.on_delivery_deleted_on_velide(order)
        self.delivery_update.emit(order.internal_id, DeliveryRowStatus.CANCELLED)

    def on_delivery_route_started_in_velide(self, order: Order):
        self.logger.debug("Solicitando strategy para lidar com a entrega em rota.")
        self._active_strategy.on_delivery_route_started_on_velide(order)
        self.delivery_update.emit(order.internal_id, DeliveryRowStatus.IN_PROGRESS)
    
    def on_delivery_route_ended_in_velide(self, order: Order):
        self.logger.debug("Solicitando strategy para lidar com o pedido entregue.")
        self._active_strategy.on_delivery_route_ended_on_velide(order)
        self.delivery_update.emit(order.internal_id, DeliveryRowStatus.DELIVERED)

    def _sanitize_order_id(self, order: Order):
        """
        Force the internal_id to be a clean string integer (no decimals).
        Fixes mismatch between Strategy (Float) and Persistence (Int/Str).
        """
        try:
            # "650257.0" -> 650257 -> "650257"
            clean_id = str(int(float(order.internal_id)))
            order.internal_id = clean_id
        except (ValueError, TypeError):
            # Fallback if it's alphanumeric like "ORD-A1"
            order.internal_id = str(order.internal_id)