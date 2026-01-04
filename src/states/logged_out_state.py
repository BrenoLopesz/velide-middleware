from __future__ import annotations
from PyQt5.QtCore import QState, pyqtSignal

from typing import TYPE_CHECKING
from states.device_flow_state import DeviceFlowState

if TYPE_CHECKING:
    from models.app_context_model import Services


class LoggedOutState(QState):
    on_request_device_flow = pyqtSignal()

    def __init__(self, services: Services, parent=None):
        super().__init__(parent)

        self.initial_state = QState(self)
        self.device_flow_state = DeviceFlowState(services, self)
        self.initial_state.addTransition(
            self.on_request_device_flow, self.device_flow_state
        )
        self.setInitialState(self.initial_state)
