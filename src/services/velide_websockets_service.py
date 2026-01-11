import logging
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from services.auth_service import AuthService
from utils.connection_state import ConnectionState
from workers.velide_websockets_worker import VelideWebsocketsWorker
from config import ApiConfig
from models.velide_websockets_models import LatestAction

class VelideWebsocketsService(QObject):
    """
    Service layer that manages the lifecycle of the WebSocket Worker.
    Acts as a bridge/adapter between the Presenter/FSM and the Worker.
    """

    # FSM Signals
    sig_connecting = pyqtSignal()
    sig_connected = pyqtSignal()
    sig_disconnected = pyqtSignal()
    sig_error = pyqtSignal()

    # Data Signal
    action_received = pyqtSignal(LatestAction)
    
    # Lifecycle Signals
    started = pyqtSignal()
    service_stopped = pyqtSignal()

    def __init__(self, api_config: ApiConfig, auth_service: AuthService):
        """
        Initializes the service with configuration and authentication dependencies.
        """
        super().__init__()
        self.api_config = api_config
        self.auth_service = auth_service 
        
        self._worker: Optional[VelideWebsocketsWorker] = None
        self._thread_pool = QThreadPool.globalInstance()
        self.logger = logging.getLogger(__name__)

    @property
    def is_running(self) -> bool:
        """Returns True if the worker instance exists and is active."""
        return self._worker is not None

    def start_service(self):
        """
        Creates and starts a new WebSocket worker if one is not already running.
        """
        if self.is_running: 
            self.logger.warning(
                "Tentativa de iniciar o WebSocket Service, mas ele já foi iniciado."
            )
            return

        self.logger.debug("Iniciando serviço de WebSockets Velide...")
        self.started.emit()
        
        # Inject AuthService and Config into Worker
        self._worker = VelideWebsocketsWorker(self.api_config, self.auth_service)
        
        # Connect Data & Lifecycle Signals
        self._worker.signals.action_received.connect(self.action_received.emit)
        self._worker.signals.finished.connect(self._on_worker_finished)
        
        # Connect Status & Error Signals
        self._worker.signals.connection_state_changed.connect(self._dispatch_status_signal)
        # CHANGED: Connect directly to the signal that accepts string
        self._worker.signals.error_occurred.connect(self.sig_error.emit)
        
        # Launch in background thread
        self._thread_pool.start(self._worker)

    def stop_service(self):
        """
        Signals the running worker to stop. The actual cleanup happens asynchronously
        in `_on_worker_finished`.
        """
        if self._worker:
            self.logger.debug("Solicitando parada do serviço WebSocket...")
            self._worker.stop()
        else:
            self.logger.debug("Stop chamado, mas nenhum worker está ativo.")

    def _dispatch_status_signal(self, state: ConnectionState):
        """
        Translates internal ConnectionState enum to specific FSM signals.
        """
        if state == ConnectionState.CONNECTING: 
            self.sig_connecting.emit()
        elif state == ConnectionState.CONNECTED: 
            self.sig_connected.emit()
        elif state == ConnectionState.DISCONNECTED: 
            self.sig_disconnected.emit()
        elif state == ConnectionState.ERROR: 
            self.sig_error.emit()

    def _on_worker_finished(self):
        """
        Cleanup callback triggered when the worker thread actually exits.
        """
        self.logger.debug("Worker WebSocket encerrado completamente.")
        self._worker = None
        self.service_stopped.emit()