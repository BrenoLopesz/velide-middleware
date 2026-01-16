from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, pyqtSlot, QTimer
from config import AuthenticationConfig
from typing import Optional

import jwt
import time

from workers.authorization_flow_worker import AuthorizationFlowWorker
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker
from workers.refresh_token_worker import RefreshTokenWorker

from api.velide_gateway import VelideGateway

class AuthService(QObject):
    loading = pyqtSignal()
    device_code = pyqtSignal(dict)
    access_token = pyqtSignal(str)
    error = pyqtSignal(str, str)
    device_code_requested = pyqtSignal() 

    def __init__(
        self, 
        auth_config: AuthenticationConfig, 
        velide_gateway: VelideGateway,
        parent=None
    ):
        super().__init__(parent)
        self._auth_config = auth_config
        self._gateway = velide_gateway

        self._thread_pool = QThreadPool.globalInstance()
        self._authorization_flow_worker = None
        self._stored_token_retriever_worker = None
        self._refresh_token_worker = None

        # The Timer that will trigger the refresh automatically
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._on_refresh_timer_triggered)

    def request_device_code_screen(self):
        """ Emits a signal to start the auth flow. """
        self.device_code_requested.emit()

    def load_device_flow(self):
        """Creates and connects the authorization worker."""
        self.loading.emit()
        worker = AuthorizationFlowWorker(self._auth_config)
        worker.signals.device_code.connect(self.device_code)
        # Connect to internal handler, not directly to signal
        worker.signals.authenticated.connect(self._on_access_token_received)
        worker.signals.error.connect(self.error)
        self._thread_pool.start(worker)

    @pyqtSlot()
    def load_stored_token(self):
        """Creates and connects the token retriever worker."""
        worker = StoredTokenRetrieverWorker()
        # If stored token is expired, try to refresh it immediately
        worker.signals.expired.connect(self._manual_refresh_token)
        worker.signals.token.connect(self._on_access_token_received)
        # The AsyncTokenProvider is listening to this!
        worker.signals.error.connect(
            lambda msg: self.error.emit("Erro ao buscar token armazenado", msg)
        )
        self._thread_pool.start(worker)

    # --- Internal Logic & The "Loop" ---

    def _on_access_token_received(
        self, 
        access_token: str, 
        refresh_token: Optional[str] = None
    ):
        """
        THE CENTRAL HUB.
        Called whenever we get a valid token (from Login, Storage, or Refresh).
        """
        # 1. Update the Gateway immediately
        # All services (Reconciliation, etc.) now see the new token instantly.
        self._gateway.update_token(access_token)

        # 2. Update our specific Refresh Token (if a new one came in)
        if refresh_token:
            self._current_refresh_token = refresh_token

        # 3. Schedule the NEXT refresh automatically
        self._schedule_next_refresh(access_token)

        # 4. Notify the Application (FSM)
        # This keeps the FSM in "LoggedInState" (or transitions it there)
        self.access_token.emit(access_token)

    def _schedule_next_refresh(self, access_token: str):
        """Decodes JWT and sets the timer."""
        try:
            # Decode without verification (we just want the 'exp' claim)
            decoded = jwt.decode(access_token, options={"verify_signature": False})
            exp_timestamp = decoded.get("exp")

            if not exp_timestamp:
                return 

            current_time = time.time()
            buffer_seconds = 60  # Refresh 1 minute before actual death
            
            seconds_until_refresh = exp_timestamp - current_time - buffer_seconds

            if seconds_until_refresh <= 0:
                # Edge case: Token expired while app was sleeping, or close to it.
                # Refresh immediately.
                self._on_refresh_timer_triggered()
            else:
                # Convert to milliseconds for QTimer
                ms_until_refresh = int(seconds_until_refresh * 1000)
                self._refresh_timer.start(ms_until_refresh)

        except jwt.DecodeError:
            # If we can't read the token, we can't auto-refresh. 
            # We just wait for the inevitable 401 error in the workers.
            pass

    def _on_refresh_timer_triggered(self):
        """Called automatically by QTimer when time is up."""
        if self._current_refresh_token:
            self._manual_refresh_token(self._current_refresh_token)
        else:
            # No refresh token? We can't do anything. 
            # Wait for access_token to die and FSM to handle the error.
            pass

    def _manual_refresh_token(self, refresh_token: str):
        """
        Performs the refresh. 
        Previously named '_refresh_token', renamed to be more explicit.
        """
        worker = RefreshTokenWorker(refresh_token, self._auth_config)
        
        # CHANGED: Success loops back to _on_access_token_received
        worker.signals.token.connect(self._on_access_token_received)
        
        worker.signals.error.connect(
            # If refresh fails, we might need to logout.
            lambda msg: self.error.emit("Sessão expirada. Faça login novamente.", msg)
        )
        self._thread_pool.start(worker)
