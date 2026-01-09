import logging
from typing import Dict, Set, Optional, List, Tuple, Any, Union
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

# Import your models/enums
from api.sqlite_manager import DeliveryStatus
from services.daily_cleanup_service import DailyCleanupService
from services.sqlite_service import SQLiteService

RawID = Union[str, float, int]


class TrackingPersistenceService(QObject):
    """
    Manages the state of tracked deliveries using a Cache-Aside pattern.

    It maintains an in-memory cache for instant lookups (required by the
    polling strategy) while asynchronously persisting changes to SQLite
    via the SQLiteService.

    CRITICAL: This service normalizes all ERP IDs.
    Input: 12345.0 (Float) -> Stored/Cached: "12345" (String).
    """

    # Emitted when the initial data load from SQLite is complete.
    hydrated = pyqtSignal()

    def __init__(
            self, 
            sqlite_service: SQLiteService, 
            sqlite_days_retention: Optional[int] =30,
            parent=None
        ):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self._sqlite = sqlite_service
        self._cleanup_service = DailyCleanupService(
            sqlite_service=self._sqlite, 
            days_retention=sqlite_days_retention
        )
        # Start it once (e.g., when the main window shows)
        # This will run a prune immediately, and then again every 24h.
        self._cleanup_service.start_routine()

        # --- In-Memory Cache ---

        # Maps Normalized Internal ID ("623604") -> 
        # External ID (Velide UUID) (ACTIVE ONLY)
        self._status_cache: Dict[str, DeliveryStatus] = {}

        # Set of Normalized Internal IDs that are finished/cancelled
        # Acts as a "Do Not Ingest" blocklist.
        self._archived_ids: Set[str] = set()

        # Maps Normalized Internal ID ("623604") -> External ID (Velide UUID)
        self._id_map: Dict[str, str] = {}

        # --- Connect Signals ---
        self._sqlite.all_deliveries_found.connect(self._on_initial_data_loaded)

    def initialize(self):
        """Starts the hydration process."""
        self.logger.info("Buscando entregas armazenadas...")

        # We need ALL deliveries to build both the active and the blocklist
        self._sqlite.request_get_all_deliveries()

    # --- HELPER: ID NORMALIZATION ---
    def _normalize_id(self, raw_id: Any) -> str:
        """
        Standardizes the ID format to avoid mismatched cache keys.
        Logic: Float(10.0) -> Int(10) -> Str("10").

        Handles:
          - 623604.0 (float) -> "623604"
          - "623604.0" (str) -> "623604"
          - 623604 (int)     -> "623604"
        """
        try:
            return str(int(float(raw_id)))
        except (ValueError, TypeError):
            self.logger.error(f"Falha ao normalizar ID: {raw_id}. Usando string crua.")
            return str(raw_id)

    @pyqtSlot(list)
    def _on_initial_data_loaded(self, rows: List[Tuple[str, str, DeliveryStatus]]):
        """
        [SLOT] Handles the result of get_all_deliveries.
        Populates the in-memory cache using NORMALIZED keys.
        Segregates Active vs Archived based on Status.
        """
        count_active = 0
        # count_archived = 0

        for external_id, internal_id, status in rows:
            norm_id = self._normalize_id(internal_id)

            is_terminal = (
                status == DeliveryStatus.CANCELLED
                or status == DeliveryStatus.DELIVERED
                or status == DeliveryStatus.FAILED
                or status == DeliveryStatus.MISSING
            )

            if not is_terminal:
                # ACTIVE: Goes to Cache (UI + Polling)
                self._status_cache[norm_id] = status
                self._id_map[norm_id] = external_id
                count_active += 1
            else:
                # TERMINAL: Goes to Archive (Blocklist only)
                self._archived_ids.add(norm_id)
                # We also map external ID just in case we need to reference it later
                # (Optional, depending on if you need to delete archived items)
                self._id_map[norm_id] = external_id
                # count_archived += 1

        self.logger.info(
            f"Entregas recuperadas. {count_active} entregas carregadas na memória."
        )
        self.hydrated.emit()

    # --- Public API for Strategy ---

    def reserve_id(self, internal_id: RawID) -> bool:
        """
        Optimistically reserves an Internal ID in memory.
        """
        norm_id = self._normalize_id(internal_id)

        if norm_id in self._status_cache:
            self.logger.debug(f"ID {norm_id} já está sendo processado ou rastreado.")
            return False

        # Mark as PENDING in memory
        self._status_cache[norm_id] = DeliveryStatus.PENDING
        self.logger.debug(f"ID {norm_id} reservado em memória (In-Flight).")
        return True

    def release_reservation(self, internal_id: RawID):
        """
        Releases the reservation if the API call fails.
        """
        norm_id = self._normalize_id(internal_id)

        # Only delete if it exists AND we haven't mapped it to an external ID yet
        # (meaning it failed before we could save it to DB)
        if norm_id in self._status_cache and norm_id not in self._id_map:
            del self._status_cache[norm_id]
            self.logger.warning(f"Reserva do ID {norm_id} removida (Rollback).")

    def is_tracked(self, internal_id: RawID) -> bool:
        """
        Checks if the ID is known to the system (Active OR Archived).
        This stops the Ingestor from re-fetching old stuff.
        """
        norm_id = self._normalize_id(internal_id)
        return (norm_id in self._status_cache) or (norm_id in self._archived_ids)

    def get_current_status(self, internal_id: RawID) -> Optional[DeliveryStatus]:
        """
        Returns the last known status or None if not found.
        """
        norm_id = self._normalize_id(internal_id)
        return self._status_cache.get(norm_id)

    def get_active_cache_snapshot(self) -> List[Tuple[str, str, DeliveryStatus]]:
        """
        Returns a snapshot of the current in-memory active cache.

        Returns:
            List of (internal_id, external_id, status)
        """
        snapshot = []
        for norm_id, status in self._status_cache.items():
            ext_id = self._id_map.get(norm_id)
            if ext_id:
                snapshot.append((norm_id, ext_id, status))
        return snapshot

    def get_tracked_ids(self) -> List[float]:
        """
        Returns ONLY Active IDs for the Status Tracker to poll.
        """
        return [float(k) for k in self._status_cache.keys()]

    def register_new_delivery(
        self, internal_id: RawID, external_id: str, status: DeliveryStatus
    ):
        """
        Promotes a 'Reserved' ID to a fully 'Persisted' ID.
        """
        norm_id = self._normalize_id(internal_id)

        # RACE CONDITION FIX:
        # Check normalized ID in cache
        current_cached_status = self._status_cache.get(norm_id)

        # If cache has moved past PENDING 
        # (e.g., via a poll that happened fast), trust cache.
        final_status = status
        if current_cached_status and current_cached_status != DeliveryStatus.PENDING:
            final_status = current_cached_status
            self.logger.debug(
                f"ID {norm_id}: Salvando com status atualizado "
                f"'{final_status.name}' em vez do inicial."
            )

        # 1. Update Memory
        self._status_cache[norm_id] = final_status
        self._id_map[norm_id] = external_id

        # TODO: Check if this was succesful. Rollback everything if not.
        # 2. Async Persist to SQLite (Sending the CLEAN STRING ID)
        self._sqlite.request_add_delivery_mapping(
            external_id=external_id, internal_id=norm_id, status=final_status
        )

    # Update the method signature to accept deliveryman_id
    def update_status(
        self,
        internal_id: RawID,
        new_status: DeliveryStatus,
        deliveryman_id: Optional[str] = None,
    ):
        """
        Updates an existing delivery status.
        """
        norm_id = self._normalize_id(internal_id)

        if norm_id not in self._status_cache:
            self.logger.warning(
                f"Tentativa de atualizar status de ID não rastreado: {norm_id}"
            )
            return

        # 1. Memory
        self._status_cache[norm_id] = new_status

        # 2. Async Persist
        ext_id = self._id_map.get(norm_id)
        if ext_id:
            if deliveryman_id:
                self.logger.info(
                    f"Atualizando ID {norm_id}: "
                    f"Status={new_status.name}, "
                    f"EntregadorID={deliveryman_id}"
                )

            self._sqlite.request_update_delivery_status(
                external_id=ext_id, new_status=new_status, deliveryman_id=deliveryman_id
            )

        else:
            self.logger.error(
                "Erro de integridade: ID Interno %s "
                "existe no cache mas sem ID Externo.",
                norm_id
            )

    def get_external_id(self, internal_id: RawID) -> Optional[str]:
        """
        Retrieves the External ID mapped to the given Internal ID.
        """
        norm_id = self._normalize_id(internal_id)
        return self._id_map.get(norm_id)

    def stop_tracking(self, internal_id: RawID):
        """
        Moves an ID from Active Cache to Archive.
        """
        norm_id = self._normalize_id(internal_id)

        if norm_id in self._status_cache:
            # Remove from Active
            del self._status_cache[norm_id]
            # Add to Archive (so Ingestor doesn't pick it up again)
            self._archived_ids.add(norm_id)

            self.logger.debug(f"ID {norm_id} movido para arquivo (Stop Tracking).")

    # --- Extensions for FarmaxStatusTracker Compatibility ---

    def get_active_monitored_ids(self) -> List[float]:
        """
        Returns IDs that need status checking.
        """
        terminal_states = (
            DeliveryStatus.DELIVERED.value,
            DeliveryStatus.FAILED.value,
            DeliveryStatus.CANCELLED.value,
            DeliveryStatus.MISSING.value,
        )

        return [
            float(k[0]) for k in self._status_cache.items() 
            if k[1] not in terminal_states
        ]

    def mark_as_cancelled(self, internal_id: RawID):
        """
        Updates status to CANCELLED and stops tracking to prevent further polls.
        """
        # 1. Persist the status change
        self.update_status(internal_id, DeliveryStatus.CANCELLED)
        # 2. Remove from polling cache
        self.stop_tracking(internal_id)

    def mark_as_finished(self, internal_id: RawID):
        """
        Updates status to DELIVERED and stops tracking.
        """
        # 1. Persist the status change
        self.update_status(internal_id, DeliveryStatus.DELIVERED)
        # 2. Remove from polling cache
        self.stop_tracking(internal_id)

    def mark_as_missing(self, internal_id: RawID):
        """
        Updates status to CANCELLED and stops tracking to prevent further polls.
        """
        # 1. Persist the status change
        self.update_status(internal_id, DeliveryStatus.MISSING)
        # 2. Remove from polling cache
        self.stop_tracking(internal_id)
