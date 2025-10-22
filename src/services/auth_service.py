from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from config import AuthenticationConfig

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

        self._thread_pool = QThreadPool.globalInstance()
        self._authorization_flow_worker = None
        self._stored_token_retriever_worker = None
        self._refresh_token_worker = None

    def load_device_flow(self):
        """Creates and connects the authorization worker."""
        worker = AuthorizationFlowWorker(self._auth_config)
        # Connect the worker's specific signals here
        worker.signals.device_code.connect(self.device_code)
        worker.signals.authenticated.connect(self.access_token)
        worker.signals.error.connect(self.error)
        self._thread_pool.start(worker)

    def load_stored_token(self):
        """Creates and connects the token retriever worker."""
        worker = StoredTokenRetrieverWorker()
        worker.signals.expired.connect(self._refresh_token)
        worker.signals.token.connect(self.access_token)
        self._thread_pool.start(worker)
    
    def _refresh_token(self, refresh_token):
        """Creates and connects the token retriever worker."""
        worker = RefreshTokenWorker(refresh_token, self._auth_config)
        worker.signals.token.connect(self.access_token)
        self._thread_pool.start(worker)