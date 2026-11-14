import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import List

from PyQt5.QtCore import pyqtSignal, QObject, QThreadPool, QTimer

from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
from connectors.farmax.identity import normalize_farmax_id
from connectors.farmax.cancellation import cancellation_progress, CancellationDecision

# We must import the model to type-hint the callback correctly
from models.farmax_models import FarmaxSale
from services.tracking_persistence_service import TrackingPersistenceService


@dataclass
class FarmaxTrackerConfig:
    """Configuration for the status tracking behavior."""

    poll_interval_ms: int = (
        60 * 1000
    )  # 60 Seconds (Checks are less urgent than new orders)
    batch_size: int = 50  # Max number of IDs to query at once to prevent SQL overload


class FarmaxStatusTracker(QObject):
    """
    Responsible for monitoring the status of ACTIVE orders.

    Unlike the Ingestor (which looks for NEW rows), this service:
    1. Retrieves a list of 'active' IDs from the Persistence Service.
    2. Queries Farmax to get the current status of those specific IDs.
    3. Reports cancellation progress; completion remains owned by Velide.
    """

    # Signals
    order_cancelled = pyqtSignal(
        str, object
    )  # Emits internal_id (str), External ID (or None)
    error_occurred = pyqtSignal(str)
    review_required = pyqtSignal(str, str, object)
    source_active = pyqtSignal(str)
    # (canonical sale ID, JSON-safe complete snapshot/counts/decision).
    # Consumers persist this before acknowledging any cancellation request.
    cancellation_progress = pyqtSignal(str, object)

    def __init__(
        self,
        repository: FarmaxRepository,
        persistence: TrackingPersistenceService,
        config: FarmaxTrackerConfig = FarmaxTrackerConfig(),
    ):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._repository = repository
        self._persistence = persistence
        self._config = config

        self._thread_pool = QThreadPool.globalInstance()
        self._is_running = False
        self._is_processing = False  # Semaphore to prevent overlapping poll cycles
        self._pending_workers = 0
        self._generation = 0
        self._last_logged_progress = {}

        # Timer
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._execute_poll_cycle)

    # --- Public Interface ---

    def start(self) -> None:
        """Starts the status monitoring process."""
        if self._is_running:
            self._logger.warning("O Rastreador de Status já está em execução.")
            return

        self._is_running = True
        self._generation += 1
        self._is_processing = False
        self._logger.debug("Iniciando o Rastreador de Status Farmax...")

        # Run immediately, then schedule
        self._execute_poll_cycle()
        self._poll_timer.start(self._config.poll_interval_ms)

    def stop(self) -> None:
        """Stops the monitoring timer."""
        self._logger.info("Parando o Rastreador de Status Farmax...")
        self._is_running = False
        self._generation += 1
        self._poll_timer.stop()

    # --- Polling Logic ---

    def _execute_poll_cycle(self) -> None:
        """
        Main loop step.
        1. Gets active IDs.
        2. Spawns workers to check them.
        """
        if not self._is_running:
            return

        if self._is_processing:
            self._logger.debug(
                "Ciclo de rastreamento anterior ainda em andamento. Pulando..."
            )
            return

        try:
            self._is_processing = True
            self._generation += 1

            # 1. Get IDs that the system thinks are "Active" (Not Delivered/Cancelled)
            active_ids = self._persistence.get_active_monitored_ids()
            active_keys = {normalize_farmax_id(value) for value in active_ids}
            self._last_logged_progress = {key: value for key, value in self._last_logged_progress.items()
                                          if key in active_keys}

            if not active_ids:
                self._logger.debug("Nenhum pedido ativo para monitorar.")
                self._is_processing = False
                return

            self._logger.debug(
                f"Monitorando status de {len(active_ids)} pedidos ativos..."
            )

            # 2. Batch processing (in case we have hundreds of active orders)
            batches = [
                active_ids[i : i + self._config.batch_size]
                for i in range(0, len(active_ids), self._config.batch_size)
            ]

            self._pending_workers = len(batches)
            for batch in batches:
                self._spawn_worker_for_batch(batch)

        except Exception:
            self._logger.exception("Erro inesperado ao iniciar ciclo de rastreamento.")
            self._is_processing = False

    def _spawn_worker_for_batch(self, batch_ids: List[float]) -> None:
        """Creates a worker to check a specific subset of IDs."""

        worker = FarmaxWorker.for_fetch_sales_statuses_by_id(
            self._repository, cd_vendas=tuple(batch_ids)
        )

        generation = self._generation
        worker.signals.success.connect(lambda rows: self._on_statuses_retrieved(rows) if generation == self._generation else None)
        worker.signals.error.connect(lambda error: self._on_worker_error(error) if generation == self._generation else None)

        # We perform cleanup of the semaphore when the LAST worker finishes?
        # For simplicity in this non-blocking UI context, 
        # we release semaphore on finish.
        # Ideally, we should track active workers count, 
        # but a simple release on success is usually fine
        # for low-frequency polling.
        worker.signals.finished.connect(lambda: self._set_processing_finished() if generation == self._generation else None)

        self._thread_pool.start(worker)

    def _set_processing_finished(self):
        """Releases the lock allow next timer cycle."""
        self._pending_workers = max(0, self._pending_workers - 1)
        self._is_processing = self._pending_workers > 0

    # --- Callbacks ---

    def _on_statuses_retrieved(self, sales_updates: List[FarmaxSale]) -> None:
        """
        Analyzes the statuses returned from the database.

        The Repository returns List[FarmaxSale].
        We iterate through the objects.
        """
        if not self._is_running:
            return

        try:
            by_sale = defaultdict(list)
            for item in sales_updates:
                by_sale[normalize_farmax_id(item.id)].append(item)
            for internal_id, items in by_sale.items():
                progress = cancellation_progress(items)
                self.cancellation_progress.emit(internal_id, progress)
                self._log_progress_if_changed(internal_id, progress)
                decision = CancellationDecision(progress["decision"])
                if decision == CancellationDecision.CANCEL:
                    # Only request cancellation. The durable consumer owns
                    # retries/ACK and must not archive until remote success.
                    self.order_cancelled.emit(internal_id, self._persistence.get_external_id(internal_id))
                elif decision != CancellationDecision.ACTIVE:
                    self.review_required.emit(internal_id, progress["reason"], progress["items"])
                else:
                    self.source_active.emit(internal_id)

        except Exception as e:
            self._logger.exception(
                "Erro inesperado ao processar atualizações de status."
            )
            self.error_occurred.emit(str(e))

    def _log_progress_if_changed(self, internal_id, progress):
        # The journal logs only accepted/correlated changes. Legacy callers get
        # an in-memory fallback without suppressing any poll evidence/signals.
        if getattr(self._persistence, "journal", None) is not None:
            return
        summary = tuple(progress[field] for field in (
            "decision", "reason", "total_items", "cancelled_items", "active_items",
            "returned_items", "unknown_items", "percentage", "snapshot_complete"))
        if self._last_logged_progress.get(internal_id) == summary:
            return
        self._last_logged_progress[internal_id] = summary
        if progress["decision"] == CancellationDecision.CANCEL:
            self._logger.info("Pedido %s: %s (%s/%s itens cancelados).",
                              internal_id, progress["reason"], progress["cancelled_items"], progress["total_items"])
        elif progress["decision"] != CancellationDecision.ACTIVE:
            self._logger.warning("Pedido %s: %s Aguardando nova consulta.", internal_id, progress["reason"])
        elif progress["cancelled_items"]:
            self._logger.info("Pedido %s: %s/%s itens cancelados (%.1f%%); entrega permanece ativa.",
                              internal_id, progress["cancelled_items"], progress["total_items"], progress["percentage"])

    def _on_worker_error(self, error_msg: str) -> None:
        """Handles DB query errors."""
        self._logger.error(f"Erro no worker de status: {error_msg}")
        # We don't stop the timer; we just wait for the next robust cycle.

    # --- Business Logic Helpers ---

    def _is_cancelled(self, status: str) -> bool:
        """Compatibility helper: a sale status alone never authorizes removal."""
        return False

    def _is_finished(self, status: str) -> bool:
        """Compatibility helper: matching source projection needs full evidence."""
        return False
