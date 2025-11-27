import logging
from typing import List, Optional, Callable

from PyQt5.QtCore import pyqtSlot

from config import FarmaxConfig
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_status_tracker import FarmaxStatusTracker
from connectors.farmax.farmax_worker import FarmaxWorker
from models.velide_delivery_models import Order
from services.strategies.connectable_strategy import IConnectableStrategy
from services.tracking_persistence_service import TrackingPersistenceService
from api.sqlite_manager import DeliveryStatus

class FarmaxStrategy(IConnectableStrategy):
    """
    The High-Level Coordinator for Farmax Integration.
    
    Acts as a Facade pattern, delegating complex polling logic to specialized
    worker services (Ingestor for new orders, Tracker for status updates).
    """

    def __init__(
        self, 
        farmax_config: FarmaxConfig, 
        farmax_repository: FarmaxRepository,
        persistence_service: TrackingPersistenceService
    ):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._config = farmax_config
        self._repository = farmax_repository
        self._persistence = persistence_service
        self._thread_pool = None # Initialized by globalInstance usually, or in workers

        # --- Initialize Sub-Services ---
        
        # 1. Ingestor: Handles finding NEW deliveries
        self._ingestor = FarmaxDeliveryIngestor(
            repository=self._repository,
            persistence=self._persistence
        )
        
        # 2. Status Tracker: Handles finding CANCELLATIONS
        # (Assumed to be refactored similarly to Ingestor)
        self._status_tracker = FarmaxStatusTracker(
            repository=self._repository,
            persistence=self._persistence
        )

        # --- Wire Signals ---
        self._connect_signals()

    def _connect_signals(self):
        """Connects internal service signals to the public Strategy signals."""
        
        # When Ingestor finds new orders -> Emit to System
        self._ingestor.orders_received.connect(self._on_orders_received)
        self._ingestor.error_occurred.connect(self._on_service_error)

        # When Tracker finds cancellations -> Emit to System
        self._status_tracker.order_cancelled.connect(self.order_cancelled.emit)
        self._status_tracker.error_occurred.connect(self._on_service_error)

    # --- Public Interface Implementation (IConnectableStrategy) ---

    def start_listening(self):
        """Starts the polling services."""
        self._logger.info("Iniciando o rastreamento Farmax...")
        
        # Ensure persistence is ready before starting workers
        self._persistence.initialize()
        
        # We start the sub-services. 
        # Note: Ideally, these wait for persistence.hydrated if strictly necessary,
        # but handled here for simplicity.
        self._ingestor.start()
        self._status_tracker.start()

    def stop_listening(self):
        """Stops all polling services."""
        self._logger.info("Parando o rastreamento Farmax...")
        self._ingestor.stop()
        self._status_tracker.stop()

    def requires_initial_configuration(self) -> bool:
        return True

    def fetch_deliverymen(self, success: Callable, error: Callable) -> None:
        """
        Fetches deliverymen using the standard Worker pattern.
        This is a simple 'one-shot' action, so it doesn't need a dedicated service class.
        """
        # We access the global thread pool here for ad-hoc tasks
        from PyQt5.QtCore import QThreadPool
        pool = QThreadPool.globalInstance()

        worker = FarmaxWorker.for_fetch_deliverymen(self._repository)
        worker.signals.success.connect(success)
        worker.signals.error.connect(error)
        pool.start(worker)

    def on_delivery_added(self, internal_id: str, external_id: str):
        """Callback: Velide API accepted the order."""
        if not self._persistence.is_tracked(float(internal_id)):
            self._logger.warning(f"Pedido {internal_id} adicionado ao Velide, mas não encontrado no rastreamento local.")
            return

        self._logger.debug(f"Integração confirmada: Farmax {internal_id} <-> Velide {external_id}")
        
        self._persistence.register_new_delivery(
            internal_id=internal_id,
            external_id=external_id,
            status=DeliveryStatus.ADDED
        )

    def on_delivery_failed(self, internal_id: Optional[float]):
        """Callback: Velide API rejected the order."""
        if internal_id:
            self._logger.warning(f"Falha na integração para o ID {internal_id}. Liberando reserva.")
            self._persistence.release_reservation(internal_id)

    # --- Internal Slots ---

    @pyqtSlot(list)
    def _on_orders_received(self, orders: List[Order]):
        """Relays list of orders from Ingestor to the main system one by one."""
        for order in orders:
            self.order_normalized.emit(order)

    @pyqtSlot(str)
    def _on_service_error(self, message: str):
        """Centralized logging for sub-service errors."""
        self._logger.error(f"Erro no rastreamento: {message}")