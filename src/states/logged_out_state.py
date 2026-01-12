from __future__ import annotations
from PyQt5.QtCore import QState, pyqtSignal

from typing import TYPE_CHECKING
from states.device_flow_state import DeviceFlowState

if TYPE_CHECKING:
    from models.app_context_model import Services


class LoggedOutState(QState):
    def __init__(self, services: Services, parent=None):
        super().__init__(parent)
        self.services = services

        self.initial_state = QState(self)
        self.device_flow_state = DeviceFlowState(services, self)
        self.initial_state.addTransition(
            self.services.auth.device_code_requested, self.device_flow_state
        )
        self.setInitialState(self.initial_state)
