# services/velide_websockets_service.py
import logging
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from typing import Optional

# Import your Worker and Config
from utils.connection_state import ConnectionState
from workers.velide_websockets_worker import VelideWebsocketsWorker
from config import ApiConfig
from models.velide_websockets_models import LatestAction


class VelideWebsocketsService(QObject):
    """
    Service layer that manages the lifecycle of the WebSocket Worker.
    Acts as a bridge/adapter between the Presenter/FSM and the Worker.
    """
    # The FSM will use these to trigger transitions directly
    sig_connecting = pyqtSignal()
    sig_connected = pyqtSignal()
    sig_disconnected = pyqtSignal()
    sig_error = pyqtSignal()

    # Data Signal
    action_received = pyqtSignal(LatestAction)
    
    # Lifecycle
    started = pyqtSignal()
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

    def _dispatch_status_signal(self, state: ConnectionState):
        """
        Takes the single source of truth (Enum) from Worker
        and distributes it as separated signals for the FSM.
        """
        if state == ConnectionState.CONNECTING:
            self.sig_connecting.emit()
        elif state == ConnectionState.CONNECTED:
            self.sig_connected.emit()
        elif state == ConnectionState.DISCONNECTED:
            self.sig_disconnected.emit()
        elif state == ConnectionState.ERROR:
            self.sig_error.emit()
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
        
        # 1. Connect Data
        self._worker.signals.action_received.connect(self.action_received.emit)
        
        # 2. Connect Status Adapter
        # We listen to the Worker's Enum and convert it to specific signals immediately
        self._worker.signals.connection_state_changed.connect(self._dispatch_status_signal)
        # 3. Cleanup
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
            # The worker will emit finished -> _on_worker_finished -> DISCONNECTED

    def _on_worker_finished(self):
        """
        Called when the QRunnable finishes its run() method (Thread dies).
        """
        self._worker = None
        
        # Ensure FSM knows we are definitely disconnected
        self.sig_disconnected.emit()
        
        self.service_stopped.emit()

    def update_token(self, new_token: str):
        """
        Updates the token. If running, restarts the service.
        """
        self.access_token = new_token
        if self.is_running:
            # Simple restart logic
            self.stop_service()
            # Logic to restart would go here, but usually requires waiting 
            # for the stop to complete (async). 
            # For simplicity, Presenter usually handles the restart logic.