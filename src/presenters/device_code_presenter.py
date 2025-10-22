# src/presenters/device_code_presenter.py

from PyQt5.QtCore import QObject, pyqtSignal
from utils.device_code import DeviceCodeDict
from visual.main_view import MainView
from services.auth_service import AuthService

class DeviceCodePresenter(QObject):
    error = pyqtSignal(str)
    authenticated = pyqtSignal(str)

    def __init__(self, auth_service: AuthService, view: MainView):
        super().__init__()
        self._auth_service = auth_service
        self._view = view

        self._auth_service.device_code.connect(self._on_device_code_received)
        self._auth_service.access_token.connect(self._on_authenticated)
        self._auth_service.error.connect(self._on_error_received)
        self._view.device_code_expired.connect(self.on_expire)

    def on_start(self):
        """Called by AppPresenter. Kicks off the business logic."""
        self._auth_service.load_device_flow()

    def _on_device_code_received(self, code_data: DeviceCodeDict):
        """Handles receiving device code and updates the view.""" 
        self._view.set_device_code_and_qr(code_data)

    def _on_authenticated(self, access_token: str):
        self.authenticated.emit(access_token)

    def on_expire(self):
        self.error.emit("Código do dispositivo expirado.")

    def _on_error_received(self, error_message: str):
        """Catches a low-level error and reports it up to AppPresenter."""
        self.error.emit(error_message)