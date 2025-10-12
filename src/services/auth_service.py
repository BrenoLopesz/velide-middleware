from PyQt5.QtCore import QObject, pyqtSignal
from config import AuthenticationConfig

from utils.run_in_thread import run_in_thread
from workers.authorization_flow_worker import AuthorizationFlowWorker
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker
from workers.refresh_token_worker import RefreshTokenWorker

class AuthService(QObject):
    device_code = pyqtSignal(dict)
    access_token = pyqtSignal(str)
    error = pyqtSignal(str, str)

    def __init__(self, auth_config: AuthenticationConfig, parent=None):
        super().__init__(parent)
        self._auth_config = auth_config

        self._thread = None
        self._authorization_flow_worker = None
        self._stored_token_retriever_worker = None
        self._refresh_token_worker = None

    @run_in_thread("authorization_flow")
    def load_device_flow(self):
        """Creates and connects the authorization worker."""
        worker = AuthorizationFlowWorker(self._auth_config)
        # Connect the worker's specific signals here
        worker.device_code.connect(self.device_code)
        worker.authenticated.connect(self.access_token)
        worker.error.connect(self.error)
        return worker

    @run_in_thread("stored_token_retriever")
    def load_stored_token(self):
        """Creates and connects the token retriever worker."""
        worker = StoredTokenRetrieverWorker()
        worker.expired.connect(self._refresh_token)
        worker.token.connect(self.access_token)
        return worker
    
    @run_in_thread("refresh_token")
    def _refresh_token(self, refresh_token):
        """Creates and connects the token retriever worker."""
        worker = RefreshTokenWorker(refresh_token, self._auth_config)
        worker.token.connect(self.access_token)
        return worker