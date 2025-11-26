import logging
from datetime import date, datetime
from typing import List, Optional, Set, Dict, Tuple

from PyQt5.QtCore import pyqtSignal, QThreadPool, QTimer
from api.sqlite_manager import DeliveryStatus, SQLiteManager
from config import ApiConfig, FarmaxConfig
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
# Import models needed for handlers and type-hinting
from models.farmax_models import FarmaxAction, FarmaxDelivery, DeliveryLog, FarmaxSale
from models.velide_delivery_models import Order
from services.strategies.connectable_strategy import IConnectableStrategy
from services.tracking_persistence_service import TrackingPersistenceService

class FarmaxStrategy(IConnectableStrategy):
    """
    Implements the IConnectableStrategy for Farmax, using a polling
    mechanism to detect new deliveries and track their status.
    """
    
    # --- Signals for the Presenter ---
    
    #: Emitted when a new delivery is detected and normalized.
    order_normalized = pyqtSignal(Order)
    
    #: Emitted when the status of a tracked delivery changes.
    #: (float: sale_id, str: new_status)
    order_status_changed = pyqtSignal(float, str)

    # --- Polling Configuration ---
    
    #: Interval (ms) to check for *new* deliveries in the log.
    NEW_DELIVERY_POLL_INTERVAL_MS = 30 * 1000  # 30 seconds
    
    #: Interval (ms) to check for *status updates* on tracked deliveries.
    STATUS_POLL_INTERVAL_MS = 5 * 1000       # 5 seconds

    def __init__(
            self, 
            farmax_config: FarmaxConfig, 
            farmax_repository: FarmaxRepository,
            persistence_service: TrackingPersistenceService
        ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.config = farmax_config
        self._farmax = farmax_repository
        self._thread_pool = QThreadPool.globalInstance() 
        self._persistence = persistence_service

        # --- Internal State ---
        self._last_check_time: Optional[datetime] = None

        # Track the last processed Log ID
        self._last_log_id: Optional[int] = None

        # --- Timers ---
        
        # Timer for polling the DELIVERYLOG for new entries
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_for_new_deliveries)
        
        # Timer for polling the VENDAS table for status changes
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_for_status_updates)

        # Connect to hydration signal
        self._persistence.hydrated.connect(self._on_persistence_ready)

    # --- Public Interface Implementation ---

    def start_listening(self):
        """
        Starts the polling mechanism.
        
        Records the current time and begins polling for new deliveries
        and tracking their status.
        """
        if self._poll_timer.isActive():
            self.logger.warning("As entregas do Farmax já estão sendo rastreadas.")
            return

        self.logger.info("Iniciando rastreamento de entregas do Farmax...")
        self._persistence.initialize()

    def _on_persistence_ready(self):
        """Called once SQLite data is loaded into memory."""
        today = date.today()
        midnight = datetime.combine(today, datetime.min.time())
        self._last_check_time = midnight
        self._last_log_id = None

        # Start Timers
        self._poll_timer.start(self.NEW_DELIVERY_POLL_INTERVAL_MS)
        self._status_timer.start(self.STATUS_POLL_INTERVAL_MS)

        self.logger.info("Rastreamento de entregas iniciado.")
        
        # Immediate Poll
        self._poll_for_new_deliveries()

    def stop_listening(self):
        """
        Stops all polling timers and clears the tracking state.
        """
        if not self._poll_timer.isActive():
            self.logger.warning("Rastreamento de entregas do Farmax não está ativado.")
            return
            
        self.logger.info("Parando rastreamento de entregas do Farmax...")
        self._poll_timer.stop()
        self._status_timer.stop()
        
        # Clear the state
        self._last_check_time = None
        self._last_log_id = None
        self._tracked_sales_ids.clear()
        self._tracked_sales_statuses.clear()

    def requires_initial_configuration(self):
        return True

    def fetch_deliverymen(self, success, error):
        """
        Asynchronously fetches the list of available deliverymen.
        
        Args:
            success (callable): Slot to connect to the worker's success signal.
            error (callable): Slot to connect to the worker's error signal.
        """
        worker = FarmaxWorker.for_fetch_deliverymen(self._farmax)
        worker.signals.success.connect(success)
        worker.signals.error.connect(error)
        self._thread_pool.start(worker)

    def stop_tracking_delivery(self, sale_id: float):
        """
        Public method to stop tracking a specific delivery.
        
        This removes the sale ID from the set of tracked IDs, so it
        will no longer be checked for status updates.
        
        Args:
            sale_id (float): The CD_VENDA of the delivery to stop tracking.
        """
        self.logger.debug(f"Removendo rastreamento da entrega: {sale_id}")
        self._tracked_sales_ids.discard(sale_id)
        self._tracked_sales_statuses.pop(sale_id, None)

    def on_delivery_added(self, internal_id: float, external_id: str):
        """
        [Callback] Called by DeliveriesService when Velide successfully accepts the order.
        """
        self.logger.debug(f"Integração confirmada: Farmax {internal_id} <-> Velide {external_id}")
        
        # This commits the relationship to SQLite and removes the "In-Flight" reservation
        self._persistence.register_new_delivery(
            internal_id=internal_id,
            external_id=external_id,
            status=DeliveryStatus.PENDING # Initial status in Velide
        )

    def on_delivery_failed(self, internal_id: float):
        """
        [Callback] Called when Velide rejects the order.
        """
        self.logger.warning(f"Falha na integração do ID {internal_id}. Ignorando entrega do Farmax.")
        self._persistence.release_reservation(internal_id)

    # --- Polling Slots (Connected to Timers) ---

    def _poll_for_new_deliveries(self):
        """
        [SLOT] Called by `_poll_timer`.
        
        Creates a worker to fetch recent changes from the DELIVERYLOG.
        """
        # STEADY STATE: We have a Log ID, so we poll for changes > ID
        if self._last_log_id is not None:
            self.logger.debug(f"Buscando pelo ID (Último ID: {self._last_log_id})...")
            
            worker = FarmaxWorker.for_fetch_recent_changes_by_id(
                self._farmax, 
                last_id=self._last_log_id
            )
            # We don't need to pass 'current_check_time' anymore for ID logic
            worker.signals.success.connect(self._handle_new_delivery_logs)
            worker.signals.error.connect(self._handle_poll_error)
            self._thread_pool.start(worker)
            return

        # Store the time *before* the query.
        # This becomes the new `_last_check_time` *after* the query succeeds.
        # Note: It could cause a race condition, but it won't be used anymore.
        current_check_time = datetime.now()
        
        self.logger.debug("Buscando novas entregas (inicial)...")
        worker = FarmaxWorker.for_fetch_recent_changes(
            self._farmax, 
            last_check_time=self._last_check_time
        )

        worker.signals.success.connect(
            lambda logs: self._handle_new_delivery_logs(logs, current_check_time)
        )
        worker.signals.error.connect(self._handle_poll_error)
        self._thread_pool.start(worker)

    def _poll_for_status_updates(self):
        """
        [SLOT] Called by `_status_timer`.
        
        Creates a worker to fetch current statuses for all tracked sales.
        """
        tracked_ids = self._persistence.get_tracked_ids()
        if not tracked_ids:
            return

        self.logger.debug(f"Buscando atualizações em {len(tracked_ids)} entrega(s)...")
        
        # Create a snapshot of the IDs to track
        ids_tuple = tuple(tracked_ids)

        worker = FarmaxWorker.for_fetch_sales_statuses_by_id(
            self._farmax, 
            cd_vendas=ids_tuple
        )
        worker.signals.success.connect(self._handle_status_updates)
        worker.signals.error.connect(self._handle_status_error)
        self._thread_pool.start(worker)

    # --- Worker Signal Handlers (Slots) ---

    def _handle_new_delivery_logs(self, logs: List[DeliveryLog], poll_time: datetime = None):
        """
        [SLOT] Handles the result of `for_fetch_recent_changes`.
        
        It filters the logs for new deliveries, finds those we aren't
        tracking yet, and launches a *new* worker to get their full details.
        
        NOTE: This assumes the `DeliveryLog` model has 'Action' and
        'CD_VENDA' fields, or similar, to identify new deliveries.
        """
        if logs:
            # 1. We found logs. Extract the highest ID.
            # Assuming DeliveryLog has an 'id' field (the primary key of the log table)
            max_id = max(log.id for log in logs)
            
            # Update the ID. This effectively switches us to "ID Mode" for the next run.
            self._last_log_id = max_id
            self.logger.debug(f"Atualizado último Log ID para: {self._last_log_id}")

        elif poll_time is not None:
            # 2. We found NO logs, but we are in "Time Mode" (Bootstrap).
            # We must advance the time window, otherwise we will keep querying 
            # the same old time range forever until a log appears.
            self._last_check_time = poll_time
            self.logger.debug(f"Início: Nenhum log encontrado. Avançando tempo para {poll_time}")
            return # Nothing to process

        if not logs:
            return

        ids_to_fetch: Set[float] = set()
        for log in logs:
            # We assume the log table has an 'Action' column 
            is_insert = getattr(log, 'action', '').upper() == FarmaxAction.INSERT.value
            sale_id = getattr(log, 'sale_id', None)
            
            if is_insert and sale_id and not self._persistence.is_tracked(sale_id):
                ids_to_fetch.add(sale_id)

        if not ids_to_fetch:
            return  # No new deliveries found

        if len(ids_to_fetch) > 1:
            self.logger.info(f"Foram detectadas {len(ids_to_fetch)} novas entregas. Buscando detalhes...")
        else:
            self.logger.info(f"Foi detectada uma nova entrega. Buscando detalhes...")
        
        # We found new IDs. Now fetch their full delivery info
        # to normalize and start tracking.
        worker = FarmaxWorker.for_fetch_deliveries_by_id(
            self._farmax, 
            cd_vendas=tuple(ids_to_fetch)
        )
        worker.signals.success.connect(self._handle_new_delivery_details)
        worker.signals.error.connect(self._handle_poll_error)
        self._thread_pool.start(worker)

    def _handle_new_delivery_details(self, deliveries: List[FarmaxDelivery]):
        """
        [SLOT] Handles the result of `for_fetch_deliveries_by_id`.
        
        Normalizes the new deliveries, emits them, and adds them to
        the tracking set.
        """
        for delivery in deliveries:
            sale_id = delivery.sale_id

            # 1. Try to Reserve the ID immediately
            # If returns False, it means we are already handling it (or it's done)
            if not self._persistence.reserve_id(sale_id):
                continue
            
            self.logger.info(f"Recebido nova entrega com o ID: {sale_id}")
            
            # 2. Normalize the FarmaxDelivery -> Order
            normalized_order = self._normalize_delivery(delivery)
            
            # 3. Emit to UI/Presenter
            # The Presenter now has the responsibility to callback 'on_integration_success'
            # or 'on_integration_failure'
            self.order_normalized.emit(normalized_order)

    def on_integration_success(self, internal_id: float, external_id: str):
        """Called by Presenter when Velide API returns 201 Created."""
        self._persistence.register_new_delivery(
            internal_id=internal_id,
            external_id=external_id,
            status=DeliveryStatus.PENDING
        )

    def on_integration_failure(self, internal_id: float):
        """Called by Presenter when Velide API fails."""
        # Release the ID so we can try again in the next poll cycle (30s later)
        self._persistence.release_reservation(internal_id)

    def _handle_status_updates(self, sales: List[FarmaxSale]):
        """
        [SLOT] Handles the result of `for_fetch_sales_statuses_by_id`.
        
        Compares the new statuses against the cached statuses and emits
        a signal for any that have changed.
        """
        for sale in sales:
            sale_id = sale.id
            new_farmax_status = sale.status
            
            # TODO: This function should be used only to detect if the delivery was cancelled.

            # Map Farmax Status Char to DeliveryStatus Enum
            # new_enum_status = self._map_sale_status(new_farmax_status)
            
            # current_enum_status = self._persistence.get_current_status(sale_id)
            
            # if current_enum_status != new_enum_status:
            #     self.logger.info(f"Mudança de status: {current_enum_status} -> {new_enum_status}")
                
            #     # Update Persistence
            #     self._persistence.update_status(sale_id, new_enum_status)
                
            #     # Emit Signal
            #     self.order_status_changed.emit(sale_id, new_farmax_status)

    def _handle_poll_error(self, error_msg: str):
        """[SLOT] Logs errors from the new delivery poll."""
        self.logger.error(f"Error polling for new deliveries: {error_msg}")
        # Depending on severity, you might want to stop the timer
        # self.stop_listening()

    def _handle_status_error(self, error_msg: str):
        """[SLOT] Logs errors from the status update poll."""
        self.logger.error(f"Error polling for status updates: {error_msg}")
        
    # --- Helper Methods ---

    def _map_sale_status(self, farmax_char: str) -> DeliveryStatus:
        """Helper to convert Farmax sale status to Enum."""
        if farmax_char.upper() == 'V':
            return DeliveryStatus.PENDING
        return DeliveryStatus.CANCELLED

    def _normalize_delivery(self, delivery: FarmaxDelivery) -> Order:
        """
        Converts a Farmax-specific model to the generic `Order` model.
        """
        order = Order(
            customerName=delivery.customer_name,
            customerContact=getattr(delivery, "customer_contact", None),
            address=delivery.address,
            neighborhood=getattr(delivery, "neighborhood", None),
            createdAt=delivery.created_at,
            reference=getattr(delivery, "reference", None)
            # ... map other fields as required by the Order model
        )

        # CRITICAL: Attach the Farmax ID to the object so it travels with the request
        order.internal_id = delivery.sale_id

        return order