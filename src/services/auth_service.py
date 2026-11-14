from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, pyqtSlot, QTimer
from config import AuthenticationConfig
from typing import Optional

import jwt
import time
from threading import RLock
from utils.token_storage import store_token_at_file

from workers.authorization_flow_worker import AuthorizationFlowWorker
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker, auth_context
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
        self._current_refresh_token = None
        self._session_generation = 0
        self._credential_revision = 0
        self._bundle_access_token = None
        self._session_lock = RLock()
        self._interactive_flow = False
        self._refreshing = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._on_refresh_timer_triggered)

    def invalidate_session(self):
        with self._session_lock:
            self._session_generation += 1
            self._credential_revision += 1
            self._bundle_access_token = None
            self._current_refresh_token = None
            self._refreshing = False
            self._refresh_timer.stop()
            if self._authorization_flow_worker:
                self._authorization_flow_worker.stop()
            self._gateway.invalidate_session()

    def _commit_token(self, generation, bundle, credential_revision=None):
        with self._session_lock:
            if generation != self._session_generation or (credential_revision is not None and credential_revision != self._credential_revision):
                return False
            store_token_at_file(dict(bundle, middleware_auth=auth_context(self._auth_config)))
            # Fence already-running disk readers before queuing publication to Qt.
            self._credential_revision += 1
            return True

    def request_device_code_screen(self):
        """ Emits a signal to start the auth flow. """
        self.device_code_requested.emit()

    def load_device_flow(self):
        """Creates and connects the authorization worker."""
        self.loading.emit()
        self.invalidate_session()
        self._interactive_flow = True
        generation = self._session_generation
        revision = self._credential_revision
        worker = AuthorizationFlowWorker(self._auth_config, commit_token=lambda bundle: self._commit_token(generation, bundle, revision))
        self._authorization_flow_worker = worker
        worker.signals.device_code.connect(lambda code: self.device_code.emit(code) if generation == self._session_generation else None)
        # Connect to internal handler, not directly to signal
        worker.signals.authenticated.connect(lambda access, refresh: self._on_access_token_received(access, refresh, generation, revision+1))
        worker.signals.error.connect(lambda title, error: self.error.emit(title, error) if generation == self._session_generation else None)
        self._thread_pool.start(worker)

    @pyqtSlot()
    def load_stored_token(self):
        """Creates and connects the token retriever worker."""
        if self._interactive_flow:
            self.error.emit("Autenticação em andamento", "Aguardando a nova sessão")
            return
        generation = self._session_generation
        revision = self._credential_revision
        worker = StoredTokenRetrieverWorker(self._auth_config)
        # If stored token is expired, try to refresh it immediately
        worker.signals.expired.connect(lambda token: self._manual_refresh_token(token) if generation == self._session_generation and revision == self._credential_revision else None)
        worker.signals.token.connect(lambda access, refresh: self._on_access_token_received(access, refresh, generation, revision))
        # The AsyncTokenProvider is listening to this!
        worker.signals.error.connect(
            lambda msg: self.error.emit("Erro ao buscar token armazenado", msg) if generation == self._session_generation and revision == self._credential_revision else None
        )
        self._thread_pool.start(worker)

    # --- Internal Logic & The "Loop" ---

    def _on_access_token_received(
        self, 
        access_token: str, 
        refresh_token: Optional[str] = None,
        generation=None,
        credential_revision=None,
    ):
        """
        THE CENTRAL HUB.
        Called whenever we get a valid token (from Login, Storage, or Refresh).
        """
        with self._session_lock:
            if generation is not None and generation != self._session_generation:
                return
            if credential_revision is not None and credential_revision != self._credential_revision:
                return
            self._interactive_flow = False
            if access_token != self._bundle_access_token or (refresh_token and refresh_token != self._current_refresh_token):
                self._credential_revision += 1
            self._bundle_access_token = access_token
            self._gateway.update_token(access_token)
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
        if self._interactive_flow or self._refreshing:
            return
        self._refreshing = True
        generation = self._session_generation
        revision = self._credential_revision
        worker = RefreshTokenWorker(refresh_token, self._auth_config,
                                    commit_token=lambda bundle: self._commit_token(generation, bundle, revision),
                                    is_current=lambda: generation == self._session_generation and revision == self._credential_revision)
        
        # CHANGED: Success loops back to _on_access_token_received
        worker.signals.token.connect(lambda access, refresh: self._on_access_token_received(access, refresh, generation, revision+1))
        worker.signals.finished.connect(lambda: setattr(self, "_refreshing", False) if generation == self._session_generation else None)
        
        # TODO: 'error' signal is unused. Fix it.
        worker.signals.error.connect(
            # If refresh fails, we might need to logout.
            lambda msg: self.error.emit("Sessão expirada. Faça login novamente.", msg) if generation == self._session_generation else None
        )
        self._thread_pool.start(worker)
