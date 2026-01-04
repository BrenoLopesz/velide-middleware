import logging
from dataclasses import dataclass
from datetime import datetime, date
from functools import partial
from typing import List, Optional, Tuple

from PyQt5.QtCore import pyqtSignal, QObject, QThreadPool, QTimer

# Assumed imports based on previous context
from connectors.farmax.farmax_mapper import FarmaxMapper
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
from models.farmax_models import FarmaxDelivery, DeliveryLog
from models.velide_delivery_models import Order
from services.tracking_persistence_service import TrackingPersistenceService


@dataclass
class FarmaxIngestorConfig:
    """Configuration for the polling and retry behavior."""

    poll_interval_ms: int = 30 * 1000  # 30 Seconds
    max_retries: int = 3
    base_backoff_ms: int = 2000  # 2 Seconds


class _CursorState:
    """
    Encapsulates the state of the polling cursor.
    Determines whether the next poll should be based on Time or ID.
    """

    def __init__(self) -> None:
        self.last_log_id: Optional[int] = None
        self.last_check_time: Optional[datetime] = None
        self._pending_id: Optional[int] = None

    def set_initial_time(self, start_time: datetime) -> None:
        """Sets the fallback time for the initial sync."""
        self.last_check_time = start_time
        self.last_log_id = None

    def prepare_pending_cursor(self, logs: List[DeliveryLog]) -> None:
        """Calculates the next potential ID but does not commit it yet."""
        if not logs:
            return
        self._pending_id = max(log.id for log in logs)

    def commit(self) -> None:
        """Moves the pending ID to the permanent state."""
        if self._pending_id is not None:
            self.last_log_id = self._pending_id
            self._pending_id = None

    def rollback(self) -> None:
        """Discards the pending ID (used on critical failure)."""
        self._pending_id = None

    @property
    def is_steady_state(self) -> bool:
        """Returns True if we have a valid ID to poll from."""
        return self.last_log_id is not None


class FarmaxDeliveryIngestor(QObject):
    """
    Responsible for ingesting NEW deliveries from Farmax.

    Flow:
    1. Poll DELIVERYLOG table (Time-based initially, ID-based subsequently).
    2. Filter for 'INSERT' actions that are not yet tracked.
    3. Fetch full details for these IDs (with exponential backoff retry).
    4. Normalize data and emit to the system.
    """

    # Signals
    orders_received = pyqtSignal(list)  # Emits List[Order]
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        repository: FarmaxRepository,
        persistence: TrackingPersistenceService,
        config: FarmaxIngestorConfig = FarmaxIngestorConfig(),
    ) -> None:
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._repository = repository
        self._persistence = persistence
        self._config = config

        self._cursor = _CursorState()
        self._thread_pool = QThreadPool.globalInstance()
        self._is_running = False

        # Retry State
        self._retry_count = 0

        # Used to avoid race conditions with different polling threads
        self._is_processing_cycle = False

        # Timers
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._execute_poll_cycle)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)

    # --- Public Interface ---

    def start(self) -> None:
        """Starts the polling process."""
        if self._is_running:
            self._logger.warning("O ingestor já está em execução.")
            return

        self._is_running = True
        self._is_processing_cycle = False  # Reset state on fresh start
        self._cursor.set_initial_time(self._get_midnight_timestamp())

        self._logger.info("Iniciando o ingestor de entregas Farmax...")
        self._execute_poll_cycle()  # Immediate first run
        self._poll_timer.start(self._config.poll_interval_ms)

    def stop(self) -> None:
        """Stops polling and pending retries."""
        self._logger.info("Parando o ingestor de entregas Farmax...")
        self._is_running = False
        self._poll_timer.stop()
        self._retry_timer.stop()
        try:
            self._retry_timer.timeout.disconnect()
        except TypeError:
            pass  # No connection existed

    # --- Step 1: Polling the Log ---

    def _execute_poll_cycle(self) -> None:
        """Initiates the worker to check the logs."""
        if not self._is_running:
            return

        # Prevent overlap
        if self._is_processing_cycle:
            self._logger.debug("Ciclo anterior ainda em andamento. Pulando poll.")
            return

        self._is_processing_cycle = True

        # FIX: Grab the value into a local variable first
        last_id = self._cursor.last_log_id

        if last_id is not None:
            self._logger.debug(f"Consultando logs > ID {last_id}...")
            worker = FarmaxWorker.for_fetch_recent_changes_by_id(
                self._repository,
                 # Mypy is happy now: it knows this is 'int', not 'Optional[int]'
                last_id=last_id, 
            )
        else:
            # Ensure we have a valid fallback if time is None 
            # (though logic implies it won't be)
            check_time = self._cursor.last_check_time or self._get_midnight_timestamp()

            self._logger.debug(f"Consultando logs >= Hora {check_time}...")
            worker = FarmaxWorker.for_fetch_recent_changes(
                self._repository, last_check_time=check_time
            )

        worker.signals.success.connect(self._on_logs_retrieved)
        worker.signals.error.connect(self._on_poll_error)

        # Check if thread pool is valid (Optional safety)
        if self._thread_pool:
            self._thread_pool.start(worker)
        else:
            self._logger.critical("ThreadPool não inicializado!")

    def _on_logs_retrieved(self, logs: List[DeliveryLog]) -> None:
        """
        Callback for Step 1.
        Filters logs and decides if we need to fetch details.
        """
        if not self._is_running:
            self._is_processing_cycle = False  # Release
            return

        if not logs:
            # Nothing happened, wait for next cycle
            self._is_processing_cycle = False  # Release
            return

        # 1. Identify relevant IDs (INSERT + Not Tracked)
        # It just asks the mapper: "Give me the IDs I don't know about."
        ids_to_fetch = FarmaxMapper.filter_new_insert_ids(
            logs, is_tracked_check=self._persistence.is_tracked
        )

        # 2. Update the pending cursor (Highest ID found in logs)
        self._cursor.prepare_pending_cursor(logs)

        # 3. Branching logic
        if not ids_to_fetch:
            # We found logs (e.g., updates), but no new inserts we care about.
            # Safe to advance cursor immediately.
            self._cursor.commit()
            self._is_processing_cycle = False  # Release lock
            return

        if len(ids_to_fetch) == 1:
            self._logger.info("Detectado uma nova entrega potencial.")
        else:
            self._logger.info(
                f"Detectadas {len(ids_to_fetch)} novas entregas potenciais."
            )

        # 4. Trigger Step 2 (Fetch Details)
        self._fetch_details_payload(tuple(ids_to_fetch))

    # --- Step 2: Fetching Details (With Retry) ---

    def _fetch_details_payload(self, sale_ids: Tuple[float, ...]) -> None:
        """Initiates the worker to fetch full order details."""
        worker = FarmaxWorker.for_fetch_deliveries_by_id(
            self._repository, cd_vendas=sale_ids
        )

        worker.signals.success.connect(self._on_details_retrieved)
        # Use partial to pass the payload to the error handler for retry context
        worker.signals.error.connect(
            partial(self._on_fetch_details_error, payload=sale_ids)
        )

        if self._thread_pool:
            self._thread_pool.start(worker)

    def _on_details_retrieved(self, deliveries: List[FarmaxDelivery]) -> None:
        """
        Callback for Step 2.
        Normalizes data and commits the transaction.
        """
        if not self._is_running:
            self._is_processing_cycle = False  # Release lock
            return

        processed_orders: List[Order] = []

        try:
            for delivery in deliveries:
                # Double check reservation to prevent race conditions or duplicates
                if self._persistence.reserve_id(delivery.sale_id):
                    # Handling validation isn't needed:
                    # 'FarmaxDelivery' can ALWAYS be converted to 'Order', by design.
                    order = FarmaxMapper.to_order(delivery)
                    processed_orders.append(order)
                    self._logger.info(f"Ingerindo novo pedido: {delivery.sale_id}")

            if processed_orders:
                self.orders_received.emit(processed_orders)

            # CRITICAL: Only advance cursor after successful processing
            self._cursor.commit()

            # Reset retry logic
            self._retry_count = 0

        except Exception as e:
            self._logger.error(f"Erro crítico ao processar detalhes da entrega: {e}")
            self.error_occurred.emit(str(e))
            # Do NOT commit cursor. Next poll cycle will pick this up again.
        finally:
            # ALWAYS Release lock so the next timer tick can work
            self._is_processing_cycle = False

    def _on_fetch_details_error(
        self, error_msg: str, payload: Tuple[float, ...]
    ) -> None:
        """
        Handles network/db failures during details fetch.
        Implements Exponential Backoff.
        """
        if not self._is_running:
            self._is_processing_cycle = False  # Release lock
            return

        self._logger.warning(
            f"Falha ao buscar detalhes (Tentativa {self._retry_count + 1}): {error_msg}"
        )

        # Pause the main poll to prevent overlapping "catch-up" queries
        self._poll_timer.stop()

        if self._retry_count < self._config.max_retries:
            # DO NOT set self._is_processing = False here.
            # We are still technically "processing" this batch, just waiting.
            self._retry_count += 1
            delay = self._config.base_backoff_ms * (2 ** (self._retry_count - 1))

            self._logger.info(f"Tentando novamente em {delay}ms...")

            # Re-schedule the specific payload
            try:
                self._retry_timer.timeout.disconnect()
            except TypeError:
                pass

            self._retry_timer.timeout.connect(
                partial(self._fetch_details_payload, sale_ids=payload)
            )
            self._retry_timer.start(delay)
        else:
            # We gave up. NOW we release the lock.
            self._logger.error(
                f"Máximo de tentativas ({self._config.max_retries}) "
                "excedido. Pulando lote."
            )
            self.error_occurred.emit(
                f"Falha ao ingerir lote após tentativas: {error_msg}"
            )

            # Reset retry state
            self._retry_count = 0
            self._cursor.rollback()  # Don't advance ID

            self._is_processing_cycle = False  # <--- Release Lock Here

            # Restart main loop; the system acts as a 
            # Dead Letter Queue (tries again later)
            self._poll_timer.start(self._config.poll_interval_ms)

    def _on_poll_error(self, error_msg: str) -> None:
        """Handles failure of the initial Log Poll."""
        self._logger.error(f"Erro ao consultar adição de entregas: {error_msg}")
        self.error_occurred.emit(f"Erro na consulta de entregas: {error_msg}")
        # The main timer is interval-based, so it will try again automatically.

        # Release the lock so the timer can try again later
        self._is_processing_cycle = False

    # --- Helpers ---

    def _get_midnight_timestamp(self) -> datetime:
        """Returns the datetime for today at 00:00:00."""
        return datetime.combine(date.today(), datetime.min.time())
