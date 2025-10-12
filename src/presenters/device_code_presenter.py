# src/presenters/device_code_presenter.py

from PyQt5.QtCore import QObject, pyqtSignal
from visual.main_view import MainView # Or the specific screen widget
from services.auth_service import AuthService

class DeviceCodePresenter(QObject):
    error = pyqtSignal(str)
    authenticated = pyqtSignal(str)
    # You will also want a success signal
    # flow_completed = pyqtSignal()

    def __init__(self, auth_service: AuthService, view: MainView):
        super().__init__()
        self._auth_service = auth_service
        self._view = view

        # --- Connect signals ---
        self._auth_service.device_code.connect(self._on_device_code_received)
        self._auth_service.access_token.connect(self._on_authenticated)
        self._auth_service.error.connect(self._on_error_received)
        self._view.device_code_screen.expired.connect(self.on_expire)

    def on_start(self):
        """Called by AppPresenter. Kicks off the business logic."""
        self._auth_service.load_device_flow()

    def _on_device_code_received(self, code_data: str):
        """Handles receiving device code and updates the view.""" 
        self._view.device_code_screen.set_device_code(code_data)
        self._view.device_code_screen.display_qr_code()

    def _on_authenticated(self, access_token: str):
        self.authenticated.emit(access_token)

    def on_expire(self):
        self.error.emit("Código do dispositivo expirado.")

    def _on_error_received(self, error_message: str):
        """Catches a low-level error and reports it up to AppPresenter."""
        self.error.emit(error_message)