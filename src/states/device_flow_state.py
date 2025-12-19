from __future__ import annotations
from PyQt5.QtCore import QState, pyqtSignal

from typing import TYPE_CHECKING
from utils.device_code import DeviceCodeDict
if TYPE_CHECKING:
    from models.app_context_model import Services

class DeviceFlowState(QState):
    _device_code_stored = pyqtSignal()

    def __init__(self, services: Services, parent=None):
        super().__init__(parent)
        self.services = services

        self._setup_states()
        self._setup_transitions()

    def _setup_states(self):
        self.idle_state = QState(self)
        self.loading_state = QState(self)
        self.waiting_for_login = QState(self)
        self.setInitialState(self.idle_state)

    def _setup_transitions(self):
        # Idle → Loading
        self.idle_state.addTransition(self.services.auth.loading, self.loading_state)
        # Loading → Waiting for Login
        self.services.auth.device_code.connect(self._on_device_code_received)
        self.loading_state.addTransition(self._device_code_stored, self.waiting_for_login)
        # TODO: Expired or error

    def _on_device_code_received(self, code_data: DeviceCodeDict):
        self.waiting_for_login.setProperty("device_code", code_data)
        self._device_code_stored.emit()