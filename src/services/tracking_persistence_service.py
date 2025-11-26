import logging
from typing import Dict, Set, Optional, List, Tuple
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

# Import your models/enums
from api.sqlite_manager import DeliveryStatus
from services.sqlite_service import SQLiteService

class TrackingPersistenceService(QObject):
    """
    Manages the state of tracked deliveries using a Cache-Aside pattern.
    
    It maintains an in-memory cache for instant lookups (required by the 
    polling strategy) while asynchronously persisting changes to SQLite 
    via the SQLiteService.
    """
    
    # Emitted when the initial data load from SQLite is complete.
    # The strategy should wait for this before starting its timers.
    hydrated = pyqtSignal()

    def __init__(self, sqlite_service: SQLiteService, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self._sqlite = sqlite_service
        
        # --- In-Memory Cache ---
        # Maps Internal ID (Farmax) -> DeliveryStatus
        self._status_cache: Dict[str, DeliveryStatus] = {}
        
        # Maps Internal ID (Farmax) -> External ID (Velide)
        # Used to update the correct row in SQLite when status changes
        self._id_map: Dict[str, str] = {}

        # --- Connect Signals ---
        self._sqlite.all_deliveries_found.connect(self._on_initial_data_loaded)

    def initialize(self):
        """Starts the hydration process."""
        self.logger.info("Buscando entregas armazenadas...")
        self._sqlite.request_get_all_deliveries()


    @pyqtSlot(list)
    def _on_initial_data_loaded(self, rows: List[Tuple[str, str, DeliveryStatus]]):
        """
        [SLOT] Handles the result of get_all_deliveries.
        Populates the in-memory cache.
        """
        count = 0
        for external_id, internal_id, status in rows:
            # Ensure internal_id is string for consistent dict keys
            str_internal = str(internal_id)
            self._status_cache[str_internal] = status
            self._id_map[str_internal] = external_id
            count += 1
            
        if count > 0:            
            self.logger.info(f"Entregas recuperadas. {count} entregas carregadas na memória.")
        self.hydrated.emit()

    # --- Public API for Strategy ---

    def reserve_id(self, internal_id: float) -> bool:
        """
        Optimistically reserves an Internal ID in memory to prevent re-processing.
        
        This state is NOT saved to SQLite yet because we lack the External ID.
        If the app crashes here, that is fine: the reservation is lost, 
        and the order will be naturally re-detected on the next restart.
        
        Returns:
            bool: True if reserved successfully, False if already tracked.
        """
        s_id = str(internal_id)
        
        if s_id in self._status_cache:
            self.logger.debug(f"ID {internal_id} já está sendo processado ou rastreado.")
            return False

        # We mark it as PENDING in memory, but we do NOT add it to _id_map yet
        # because we don't have the Velide ID.
        self._status_cache[s_id] = DeliveryStatus.PENDING
        self.logger.debug(f"ID {internal_id} reservado em memória (In-Flight).")
        return True
    
    def release_reservation(self, internal_id: float):
        """
        Releases the reservation if the API call fails. 
        Allows the strategy to pick it up again in the next poll.
        """
        s_id = str(internal_id)
        if s_id in self._status_cache and s_id not in self._id_map:
            del self._status_cache[s_id]
            self.logger.warning(f"Reserva do ID {internal_id} removida (Rollback).")

    def is_tracked(self, internal_id: float) -> bool:
        """
        Synchronous check: Is this ID currently being tracked?
        """
        return str(internal_id) in self._status_cache

    def get_current_status(self, internal_id: float) -> Optional[str]:
        """
        Returns the last known status char/string (e.g., 'S', 'R') 
        or None if not found.
        """
        # Note: You might need a mapping logic here if your Enum 
        # doesn't match Farmax single letters perfectly. 
        # For now, we assume the caller handles the Enum conversion logic if needed.
        # This implementation returns the Enum object stored.
        return self._status_cache.get(str(internal_id))

    def get_tracked_ids(self) -> List[float]:
        """Returns list of all internal IDs currently tracked."""
        # Convert back to float for Farmax compatibility
        return [float(k) for k in self._status_cache.keys()]

    def register_new_delivery(self, internal_id: float, external_id: str, status: DeliveryStatus):
        """
        Promotes a 'Reserved' ID to a fully 'Persisted' ID.
        """
        s_id = str(internal_id)
        
        # RACE CONDITION FIX:
        # Before saving, check if the status changed while we were waiting for Velide.
        # If the cache has a "newer" status (e.g., DELIVERED) vs the "initial_status" (PENDING),
        # we must use the cached version.
        current_cached_status = self._status_cache.get(s_id)
        
        # If we have a cached status that is NOT Pending, trust the cache.
        final_status = status
        if current_cached_status and current_cached_status != DeliveryStatus.PENDING:
            final_status = current_cached_status
            self.logger.debug(f"ID {s_id}: Salvando com status atualizado '{final_status.name}' em vez do inicial.")

        # 1. Update Memory (Promote from Reservation)
        self._status_cache[s_id] = status
        self._id_map[s_id] = external_id
        
        # 2. Async Persist to SQLite
        # Now we have the external_id, so we can satisfy the Database Constraints
        self._sqlite.request_add_delivery_mapping(
            external_id=external_id,
            internal_id=s_id,
            status=status
        )

    def update_status(self, internal_id: float, new_status: DeliveryStatus):
        """
        Updates an existing delivery status.
        1. Updates Memory Immediately.
        2. Triggers Async SQLite Update.
        """
        s_id = str(internal_id)
        
        if s_id not in self._status_cache:
            self.logger.warning(f"Tentativa de atualizar status de ID não rastreado: {internal_id}")
            return

        # 1. Memory
        self._status_cache[s_id] = new_status
        
        # 2. Async Persist
        # We need the external_id to update the SQLite table (Primary Key)
        ext_id = self._id_map.get(s_id)
        if ext_id:
            self._sqlite.request_update_delivery_status(external_id=ext_id, new_status=new_status)
        else:
            self.logger.error(f"Erro de integridade: ID Interno {s_id} existe no cache mas sem ID Externo.")

    def stop_tracking(self, internal_id: float):
        """
        Stops tracking an item (removes from memory).
        Does NOT delete from SQLite (history is kept), but updates status to CANCELLED/FINALIZED via update_status before calling this.
        """
        s_id = str(internal_id)
        if s_id in self._status_cache:
            # We keep it in SQLite, but maybe remove from active memory tracking 
            # if you don't want to poll it anymore.
            # However, for the cache-aside to work for deduplication, 
            # IT MUST REMAIN IN MEMORY if we want to prevent re-adding it.
            pass