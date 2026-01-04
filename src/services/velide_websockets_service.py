import logging
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from typing import Optional

# Import your Worker and Config
from workers.velide_websockets_worker import VelideWebsocketsWorker
from config import ApiConfig
from models.velide_websockets_models import LatestAction


class VelideWebsocketsService(QObject):
    """
    Service layer that manages the lifecycle of the WebSocket Worker.
    Acts as a bridge/adapter between the Presenter/FSM and the Worker.
    """

    # --- FSM Compatible Signals ---
    # These signals drive the FSM transitions directly
    started = pyqtSignal()  # Emitted when start_service is called
    connected = pyqtSignal()  # Emitted when WebSocket is fully open
    disconnected = pyqtSignal()  # Emitted when WebSocket closes or errors

    # Data & Error Signals
    action_received = pyqtSignal(LatestAction)
    error_occurred = pyqtSignal(str)

    # Optional: Logic signal for UI cleanup
    service_stopped = pyqtSignal()

    def __init__(self, api_config: ApiConfig):
        super().__init__()
        self.api_config = api_config
        self.logger = logging.getLogger(__name__)
        self.access_token: Optional[str] = None

        # We keep a reference to the worker to call stop() later
        self._worker: Optional[VelideWebsocketsWorker] = None

        # Use the global thread pool (efficient re-use of threads)
        self._thread_pool = QThreadPool.globalInstance()

    @property
    def is_running(self) -> bool:
        """Check if the worker is currently active."""
        return self._worker is not None

    def set_access_token(self, access_token: str):
        self.access_token = access_token

    def start_service(self):
        """
        Creates the worker, connects signals, and schedules it.
        """
        if self.access_token is None:
            self.logger.error(
                "Não foi possível conectar com o Velide: Não está autenticado ainda."
            )
            return

        if self.is_running:
            return

        # 1. Emit 'started' immediately to transition FSM to 'Connecting'
        self.started.emit()

        # 2. Instantiate the Worker
        self._worker = VelideWebsocketsWorker(self.api_config, self.access_token)

        # 3. Connect Signals

        # A. Direct Proxy: Data and Errors just pass through
        self._worker.signals.action_received.connect(self.action_received.emit)
        self._worker.signals.error_occurred.connect(self.error_occurred.emit)

        # B. Logic Adapter: Transform Boolean -> Event Signals
        # We connect the worker's boolean signal to our internal handler
        self._worker.signals.status_changed.connect(self._on_worker_status_changed)

        # C. Cleanup
        self._worker.signals.finished.connect(self._on_worker_finished)

        # 4. Execute in a separate thread
        # We explicitly prevent autoDelete so we can safely call stop() if needed,
        # though QThreadPool usually handles this well.
        # Standard QRunnable usage relies on autoDelete=True, but since we
        # hold a reference (self._worker), we must be careful.
        self._worker.setAutoDelete(True)
        self._thread_pool.start(self._worker)

    def stop_service(self):
        """
        Signals the worker to stop gracefully.
        """
        if self._worker:
            # This sets the internal flag in the worker loop to False
            self._worker.stop()
            # FSM will transition to Offline via 
            # _on_worker_finished or _on_worker_status_changed

    def _on_worker_status_changed(self, is_online: bool):
        """
        Internal Slot: Adapts the Worker's boolean status
        into semantic signals for the FSM.
        """
        if is_online:
            self.connected.emit()
        else:
            self.disconnected.emit()

    def _on_worker_finished(self):
        """
        Called when the QRunnable finishes its run() method (Thread dies).
        """
        self._worker = None

        # Ensure FSM knows we are definitely disconnected now
        self.disconnected.emit()
        self.service_stopped.emit()

    def update_token(self, new_token: str):
        """
        Updates the token. If running, restarts the service.
        """
        self.access_token = new_token
        if self.is_running:
            self.stop_service()
            # Logic to restart would go here, but usually requires waiting
            # for the stop to complete (async).
            # For simplicity, Presenter usually handles the restart logic.
