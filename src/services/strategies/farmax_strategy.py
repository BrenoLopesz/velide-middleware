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

    #: Emitted when a tracked delivery status is updated to cancelled.
    order_cancelled = pyqtSignal(float, object)
    
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

        # Internal state to hold the ID we want to commit to, but haven't yet.
        self._pending_log_id: Optional[int] = None

        # Track the last processed Log ID
        self._last_log_id: Optional[int] = None

        # --- Timers ---

        # Retry Configuration
        self._retry_count = 0
        self._max_retries = 3
        self._base_backoff_ms = 2000 # Start with 2 seconds

        # Retry Timer (Single Shot)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_logic)
        
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

    def on_delivery_added(self, internal_id: str, external_id: str):
        """
        [Callback] Called by DeliveriesService when Velide successfully accepts the order.
        """
        if not self._persistence.is_tracked(internal_id):
            # If everything is correct, the cancellation would be already requested, and will likely happen.
            self.logger.warning(f"Entrega {internal_id} foi adicionada no Velide após provavelmente ser cancelada do Farmax.")
            return

        self.logger.debug(f"Integração confirmada: Farmax {internal_id} <-> Velide {external_id}")
        
        # This commits the relationship to SQLite and removes the "In-Flight" reservation
        self._persistence.register_new_delivery(
            internal_id=internal_id,
            external_id=external_id,
            status=DeliveryStatus.ADDED
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
        if not logs:
            if poll_time:
                self._last_check_time = poll_time
            return

        # 1. Calculate the Candidate ID, but DO NOT save it to _last_log_id yet.
        max_id = max(log.id for log in logs)
        self._pending_log_id = max_id  # Store it in a temporary pending state
        
        ids_to_fetch: Set[float] = set()
        for log in logs:
            # logic to extract sale_id...
            is_insert = getattr(log, 'action', '').upper() == FarmaxAction.INSERT.value
            sale_id = getattr(log, 'sale_id', None)
            
            if is_insert and sale_id and not self._persistence.is_tracked(sale_id):
                ids_to_fetch.add(sale_id)

        if not ids_to_fetch:
            # If we found logs but none were relevant (e.g., updates vs inserts),
            # we can safely advance the cursor now because there is no payload to fetch.
            self._commit_cursor()
            return

        if len(ids_to_fetch) > 1:
            self.logger.info(f"Foram detectadas {len(ids_to_fetch)} novas entregas. Buscando detalhes...")
        else:
            self.logger.info(f"Foi detectada uma nova entrega. Buscando detalhes...")

        # 2. Trigger Phase 2
        self._fetch_details_payload(tuple(ids_to_fetch))

    def _fetch_details_payload(self, cd_vendas: Tuple[float, ...]):
        """Helper to launch the worker, separated to allow easy retrying."""
        worker = FarmaxWorker.for_fetch_deliveries_by_id(
            self._farmax, 
            cd_vendas=cd_vendas
        )
        # Pass the IDs into the worker data or use partials if needed to keep context on error
        # For simplicity, we assume we can bind the current payload to the error handler if needed,
        # but here we use instance state or lambda.
        
        worker.signals.success.connect(self._handle_new_delivery_details)
        
        # Connect error to a specialized retry handler, passing the payload
        worker.signals.error.connect(lambda err: self._handle_fetch_error(err, cd_vendas))
        
        self._thread_pool.start(worker)

    def _handle_new_delivery_details(self, deliveries: List[FarmaxDelivery]):
        """
        [SLOT] Handles the result of `for_fetch_deliveries_by_id`.
        
        Normalizes the new deliveries, emits them, and adds them to
        the tracking set.
        """
        try:
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
            
            # SUCCESS: The dangerous part is over. Now we commit the cursor.
            self._commit_cursor()
            
            # Reset retry counter on success
            self._retry_count = 0
        except Exception as e:
            self.logger.error(f"Erro crítico ao processar detalhes: {e}")
            # Do NOT commit cursor here. We will re-poll naturally.

    def _commit_cursor(self):
        """Moves the Pending ID to the Permanent Last ID."""
        if self._pending_log_id is not None:
            self._last_log_id = self._pending_log_id
            self._pending_log_id = None
            self.logger.debug(f"Cursor avançado com sucesso para Log ID: {self._last_log_id}")
            
        # Ensure main poll is running again if it was stopped
        if not self._poll_timer.isActive():
            self._poll_timer.start(self.NEW_DELIVERY_POLL_INTERVAL_MS)
            
    def _handle_fetch_error(self, error_msg: str, cd_vendas: Tuple[float, ...]):
        """
        Handles failure in fetching details. 
        Does NOT advance cursor. Initiates Retry.
        """
        self.logger.warning(f"Falha ao buscar detalhes no Farmax (Tentativa {self._retry_count + 1}/{self._max_retries}): {error_msg}")

        # Pause the main poll to prevent overlapping attempts
        self._poll_timer.stop()

        if self._retry_count < self._max_retries:
            self._retry_count += 1
            # Exponential Backoff: 2s, 4s, 8s...
            delay = self._base_backoff_ms * (2 ** (self._retry_count - 1))
            
            self.logger.info(f"Agendando nova tentativa em {delay/1000} segundos...")
            
            # Disconnect previous to avoid multi-connection if using lambda (cleaner to store current_payload)
            # Here we use a lambda in the timer for simplicity
            self._retry_timer.timeout.disconnect() 
            self._retry_timer.timeout.connect(lambda: self._fetch_details_payload(cd_vendas))
            self._retry_timer.start(delay)
        else:
            self.logger.error("Número máximo de retentativas excedido. Abortando este lote.")
            # We reset the retry count.
            self._retry_count = 0
            # Restart the main poll so we try again later
            self._poll_timer.start(self.NEW_DELIVERY_POLL_INTERVAL_MS)
            # CRITICAL DECISION:
            # 1. Do we advance the cursor? If yes, we lose data.
            # 2. Do we leave it? If yes, the main 30s timer will pick it up again.
            # Choice: Leave it. The main loop acting as a "Dead Letter Queue" is safer.
            # The system will try again in 30 seconds.

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
        a signal if it was cancelled.
        """
        for sale in sales:
            sale_id = sale.id
            new_farmax_status = sale.status
            
            if new_farmax_status.upper() not in ('C', 'D'):
                continue # Not cancelled
            
            external_id = self._persistence.get_external_id(sale_id)

            # In practice, it is almost IMPOSSIBLE that an order is cancelled before it is added to Velide.
            # Unless the polling timeouts are messed up, or if the cancellation is done extremely fast.
            # But for safety, we're going to try to handle it as well.
            if external_id is None:
                self.logger.warning(f"Entrega {sale_id} foi cancelada no Farmax antes de ser adicionada ao Velide.")
                self._persistence.release_reservation(sale_id)
            else:
                self.logger.info(f"Solicitando remoção da entrega {sale_id} cancelada no Farmax...")
            
            self.order_cancelled.emit(sale_id, external_id)

    def _handle_poll_error(self, error_msg: str):
        """[SLOT] Logs errors from the new delivery poll."""
        self.logger.error(f"Erro ao buscar entregas no Farmax: {error_msg}")
        # TODO: Crash and display big error
        # Depending on severity, you might want to stop the timer
        # self.stop_listening()

    def _handle_status_error(self, error_msg: str):
        """[SLOT] Logs errors from the status update poll."""
        self.logger.error(f"Erro ao buscar atualização nas entregas: {error_msg}")
        # TODO: Crash and display big error
        
    # --- Helper Methods ---

    def _normalize_delivery(self, delivery: FarmaxDelivery) -> Order:
        """
        Converts a Farmax-specific model to the generic `Order` model.
        """
        return Order(
            customerName=delivery.customer_name,
            customerContact=getattr(delivery, "customer_contact", None),
            address=delivery.address,
            neighborhood=getattr(delivery, "neighborhood", None),
            createdAt=delivery.created_at,
            reference=getattr(delivery, "reference", None),
            internal_id=str(delivery.sale_id)
        )