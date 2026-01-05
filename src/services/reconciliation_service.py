import time
import logging
from typing import Dict, Optional

from PyQt5.QtCore import QObject, pyqtSignal, QTimer, QThreadPool

from api.velide import Velide
from config import ApiConfig, TargetSystem
from utils.velide_status_to_local import map_velide_status_to_local
from workers.velide_worker import VelideWorker

# We import the SERVICE, not the DB Manager
from services.tracking_persistence_service import TrackingPersistenceService


class ReconciliationService(QObject):
    """
    Orchestrates synchronization between In-Memory Cache and Velide Cloud.
    Updates are pushed through TrackingPersistenceService to maintain cache coherency.
    """

    sync_started = pyqtSignal()
    sync_finished = pyqtSignal(int)  # Emits number of updates performed
    sync_error = pyqtSignal(str)

    delivery_in_route = pyqtSignal(str, str)  # Delivery ID, Deliveryman ID
    delivery_missing = pyqtSignal(str)

    # TODO: Use config
    COOLDOWN_SECONDS = 60.0
    SYNC_INTERVAL_MS = 600_000  # 10 Minutes

    def __init__(
        self,
        tracking_service: TrackingPersistenceService,
        api_config: ApiConfig,
        target_system: TargetSystem,
    ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._velide_api: Optional[Velide] = None

        # Dependency Injection
        self.tracking_service = tracking_service
        self._api_config = api_config
        self._target_system = target_system

        # 1. The Cooldown "Bouncer" List
        # Format: { "velide_id": timestamp_of_last_websocket_event }
        self._websocket_cooldowns: Dict[str, float] = {}

        # 2. Setup the Timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.trigger_reconciliation)
        # Don't start timer yet; let the main app start it.

        self._is_missing_api = False

    def set_access_token(self, access_token: str):
        self._velide_api = Velide(access_token, self._api_config, self._target_system)
        # If didn't start previosly due to missing API, start it now.
        if self._is_missing_api:
            self.start_service()

    def start_service(self):
        """Starts the automatic background timer."""
        if self._velide_api is None:
            self.logger.error(
                "É preciso estar autenticado para iniciar a reconciliação automática!"
            )
            self._is_missing_api = True
            return

        self.logger.info("Iniciando serviço de reconciliação automática.")
        # OBS: Not needed actually. Initially it won't 
        # have any deliveries to check against.
        # Ensure the cache is loaded before we try to reconcile
        # if not self.tracking_service._status_cache:
        #     self.logger.warning("Cache vazio. Aguardando hidratação...")

        self.trigger_reconciliation()
        self.timer.start(self.SYNC_INTERVAL_MS)

    def register_websocket_event(self, velide_id: str):
        """
        Call this method whenever a Websocket event arrives for a delivery.
        It marks the ID as 'hot' so the reconciler won't touch it.
        """
        self._websocket_cooldowns[velide_id] = time.time()
        self.logger.debug(f"Cooldown ativado para ID: {velide_id}")

    def trigger_reconciliation(self):
        """
        Step 1: Spawns the Worker to fetch the Global Snapshot.
        """
        self.sync_started.emit()
        self.logger.debug("Iniciando ciclo de reconciliação...")

        # Create the worker
        # Note: We don't need to pass IDs anymore, we get the Global Snapshot
        worker = VelideWorker(self._velide_api, "get_global_snapshot")

        # Connect signals
        # We need a new signal in VelideWorkerSignals: 'snapshot_retrieved'
        # Or reuse 'deliverymen_retrieved' if passing a dict/list
        worker.signals.snapshot_retrieved.connect(self._handle_snapshot_results)
        worker.signals.error.connect(self._handle_error)

        # Run in global thread pool 
        QThreadPool.globalInstance().start(worker)

    def _handle_snapshot_results(self, snapshot_map: Dict[str, tuple]):
        """
        Step 2: Compare Snapshot Data vs Local SQLite Data.

        Args:
            snapshot_map: { "velide_id": ("STATUS", "DELIVERYMAN_ID") }
        """
        updates_count = 0
        current_time = time.time()

        self.logger.debug(
            f"Snapshot recebido ({len(snapshot_map)} itens). "
            "Comparando com cache local..."
        )

        # 1. Get the "Source of Truth" from the Tracking Service
        local_active_items = self.tracking_service.get_active_cache_snapshot()

        for internal_id, velide_id, local_status in local_active_items:
            # --- CHECK 1: COOLDOWN ---
            last_ws = self._websocket_cooldowns.get(velide_id)
            if last_ws and (current_time - last_ws < self.COOLDOWN_SECONDS):
                continue

            # --- CHECK 2: ZOMBIE ---
            if velide_id not in snapshot_map:
                # Still just logging, as per strategy
                self.logger.warning(
                    f"Entrega {velide_id} ausente no Velide. Considere como "
                    "entregue ou deletada. Nenhuma ação será realizada no ERP."
                )
                self.tracking_service.mark_as_missing(internal_id)
                # Emit signal to update UI
                self.delivery_missing.emit(internal_id)
                updates_count += 1
                continue

            # --- CHECK 3: STATUS MISMATCH ---
            # Unpack the tuple from Velide._flatten_snapshot
            remote_status_str, remote_deliveryman_id = snapshot_map[velide_id]

            expected_local_enum = map_velide_status_to_local(remote_status_str)

            if expected_local_enum != local_status:
                self.logger.warning(
                    f"Encontrado inconsistência em um pedido ({internal_id}). "
                    f"Local: {local_status.name} -> Remoto: {remote_status_str}. "
                    "Aplicando correções..."
                )

                # CRITICAL: We update the SERVICE, not the DB directly.
                # This updates the in-memory cache AND triggers the async SQLite write.
                self.tracking_service.update_status(
                    internal_id,
                    expected_local_enum,
                    deliveryman_id=remote_deliveryman_id,
                )

                # Emit signal to update UI
                # Only emit deliveryman ID if status is 
                # 'ROUTED'/'IN_ROUTE' and ID exists
                if expected_local_enum.name == "IN_ROUTE" and remote_deliveryman_id:
                    self.delivery_in_route.emit(internal_id, remote_deliveryman_id)

                # (Optional) Handle other status changes via generic signal if needed

                updates_count += 1

        self._cleanup_cooldowns(current_time)
        self.logger.debug(f"Ciclo finalizado. {updates_count} correções aplicadas.")
        self.sync_finished.emit(updates_count)

    def _cleanup_cooldowns(self, current_time):
        """Remove expired entries to save memory."""
        keys_to_remove = [
            k
            for k, v in self._websocket_cooldowns.items()
            if (current_time - v) > self.COOLDOWN_SECONDS
        ]
        for k in keys_to_remove:
            del self._websocket_cooldowns[k]

    def _handle_error(self, err_msg):
        self.logger.error(f"Erro na reconciliação: {err_msg}")
        self.sync_error.emit(err_msg)
