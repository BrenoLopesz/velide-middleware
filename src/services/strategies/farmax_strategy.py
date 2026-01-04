import logging
from typing import List, Optional, Callable

from PyQt5.QtCore import pyqtSlot, QThreadPool

from config import FarmaxConfig
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor
from connectors.farmax.farmax_delivery_updater import FarmaxDeliveryUpdater
from connectors.farmax.farmax_mapper import FarmaxMapper
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_status_tracker import FarmaxStatusTracker
from connectors.farmax.farmax_worker import FarmaxWorker
from models.delivery_table_model import map_db_status_to_ui
from models.farmax_models import FarmaxDelivery
from models.velide_delivery_models import Order
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from services.reconciliation_service import ReconciliationService
from services.strategies.connectable_strategy import IConnectableStrategy
from services.tracking_persistence_service import TrackingPersistenceService
from api.sqlite_manager import DeliveryStatus
from services.velide_websockets_service import VelideWebsocketsService


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
        persistence_service: TrackingPersistenceService,
        websockets_service: VelideWebsocketsService,
        reconciliation_service: ReconciliationService,
        deliverymen_retriever: DeliverymenRetrieverService,
    ):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._config = farmax_config
        self._repository = farmax_repository
        self._persistence = persistence_service
        self._websockets = websockets_service
        self._reconciliation = reconciliation_service
        self._deliverymen_retriever = deliverymen_retriever
        self._thread_pool = None  # Initialized by globalInstance usually, or in workers

        # --- Initialize Sub-Services ---

        # 1. Ingestor: Handles finding NEW deliveries
        self._ingestor = FarmaxDeliveryIngestor(
            repository=self._repository, persistence=self._persistence
        )

        # 2. Status Tracker: Handles finding CANCELLATIONS
        # (Assumed to be refactored similarly to Ingestor)
        self._status_tracker = FarmaxStatusTracker(
            repository=self._repository, persistence=self._persistence
        )

        # 3. Updater: Handles WRITING updates to Farmax
        self._updater = FarmaxDeliveryUpdater(
            repository=self._repository,
            deliverymen_retriever=self._deliverymen_retriever,
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

        # Listen for when the SQLite DB is fully loaded
        self._persistence.hydrated.connect(self._on_persistence_hydrated)

    def _on_persistence_hydrated(self):
        """
        Triggered when TrackingPersistenceService finishes loading from SQLite.
        We must fetch the details of these deliveries to show them in the UI.
        """
        # Starts reconciliation job
        self._reconciliation.start_service()

        # Get IDs of everything currently being tracked/monitored
        tracked_ids = self._persistence.get_tracked_ids()

        if not tracked_ids:
            return

        self._logger.debug(f"Restaurando {len(tracked_ids)} entregas para a UI...")

        # 2. Reuse your existing Worker logic to fetch details from Farmax
        # We use the same worker as the Ingestor, but connect to a different slot
        worker = FarmaxWorker.for_fetch_deliveries_by_id(
            self._repository, cd_vendas=tuple(tracked_ids)
        )

        worker.signals.success.connect(self._on_restoration_details_fetched)
        worker.signals.error.connect(
            lambda err: self._logger.error(f"Erro ao restaurar entregas: {err}")
        )

        # Use the strategy's thread pool (or global)
        QThreadPool.globalInstance().start(worker)

    def _on_restoration_details_fetched(self, deliveries: List["FarmaxDelivery"]):
        """
        Callback when ERP returns details for the ID list.
        Convert them to Orders and emit order_restored.
        """
        for delivery in deliveries:
            # Convert raw ERP data to the Normalized Order Model
            # TODO: Checks if validation was succesful
            order = FarmaxMapper.to_order(delivery)

            # Get external ID
            external_id = self._persistence.get_external_id(order.internal_id)

            # Get the PERSISTED status
            current_db_status = self._persistence.get_current_status(order.internal_id)

            if current_db_status:
                order.ui_status_hint = map_db_status_to_ui(current_db_status)
            else:
                # If persistence doesn't have it (weird edge case), 
                # default to Acknowledge
                order.ui_status_hint = None

            self.order_restored.emit(order, external_id)

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

        # Subscribes to Velide Websockets
        self._websockets.start_service()

    def stop_listening(self):
        """Stops all polling services."""
        self._logger.info("Parando o rastreamento Farmax...")
        self._ingestor.stop()
        self._status_tracker.stop()
        # TODO: Stop reconciliation job

    def requires_initial_configuration(self) -> bool:
        return True

    def fetch_deliverymen(self, success: Callable, error: Callable) -> None:
        """
        Fetches deliverymen using the standard Worker pattern.
        This is a simple 'one-shot' action, so it doesn't need 
        a dedicated service class.
        """
        # We access the global thread pool here for ad-hoc tasks
        pool = QThreadPool.globalInstance()

        worker = FarmaxWorker.for_fetch_deliverymen(self._repository)
        worker.signals.success.connect(success)
        worker.signals.error.connect(error)
        pool.start(worker)

    def on_delivery_added(self, internal_id: str, external_id: str):
        """Callback: Velide API accepted the order."""
        if not self._persistence.is_tracked(float(internal_id)):
            self._logger.warning(
                f"Pedido {internal_id} adicionado ao Velide, "
                "mas não encontrado no rastreamento local."
            )
            return

        self._logger.debug(
            f"Integração confirmada: Farmax {internal_id} <-> Velide {external_id}"
        )

        self._persistence.register_new_delivery(
            internal_id=internal_id,
            external_id=external_id,
            status=DeliveryStatus.ADDED,
        )

    def on_delivery_failed(self, internal_id: Optional[float]):
        """Callback: Velide API rejected the order."""
        if internal_id:
            self._logger.warning(
                f"Falha na integração para o ID {internal_id}. Liberando reserva."
            )
            self._persistence.release_reservation(internal_id)

    def on_delivery_deleted_on_velide(self, order: Order):
        self._persistence.mark_as_cancelled(order.internal_id)
        # TODO: Add a property on the order to allow checking if
        # the cancellation was requested internally or not.
        self._logger.info(f"Uma entrega ({order.internal_id}) foi deletada no Velide.")

    def on_delivery_route_started_on_velide(
        self, order: Order, deliveryman_external_id: str
    ):
        self._persistence.update_status(order.internal_id, DeliveryStatus.IN_PROGRESS)
        self._updater.mark_as_in_route(order, deliveryman_external_id)

    def on_delivery_route_ended_on_velide(self, order):
        self._persistence.mark_as_finished(order.internal_id)
        self._logger.info(f"Pedido ({order.internal_id}) foi entregue no Velide.")
        self._updater.mark_as_done(order)

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
