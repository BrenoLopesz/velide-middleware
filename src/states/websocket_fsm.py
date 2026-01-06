# states/websocket_fsm.py
from __future__ import annotations
from PyQt5.QtCore import QObject, QStateMachine, QState, pyqtSignal
from models.velide_websockets_models import LatestAction
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from models.app_context_model import Services

class WebSocketFSM(QObject):
    """
    Manages the States of the WebSocket connection.
    Exposes QState objects (s_connecting, s_connected, etc.) 
    that the Presenter can observe.
    """
    
    # Gatekeeper signal: only emitted when in s_connected
    action_ready = pyqtSignal(LatestAction)

    def __init__(self, services: "Services"):
        """
        Args:
            service: Instance of VelideWebsocketsService
        """
        super().__init__()
        self.service = services.websockets
        self.logger = logging.getLogger(__name__)
        self.machine = QStateMachine()

        # 1. Define States (Publicly Accessible)
        self.s_root = QState()
        self.s_disconnected = QState(self.s_root)
        self.s_connecting = QState(self.s_root)
        self.s_connected = QState(self.s_root)
        self.s_error = QState(self.s_root)

        # 2. Define Transitions (Signal Driven)
        # The transition logic is now direct: "When Service emits X, go to State Y"
        
        # From any state (via Root), we can go to these states:
        self.s_root.addTransition(self.service.sig_connecting, self.s_connecting)
        self.s_root.addTransition(self.service.sig_connected, self.s_connected)
        self.s_root.addTransition(self.service.sig_disconnected, self.s_disconnected)
        self.s_root.addTransition(self.service.sig_error, self.s_error)
        
        # Specific flow (optional, if you want to restrict jumps):
        # self.s_disconnected.addTransition(self.service.started, self.s_connecting)

        # 3. Data Gatekeeper
        self.service.action_received.connect(self._handle_incoming_action)

        # 4. Init
        self.machine.addState(self.s_root)
        self.machine.setInitialState(self.s_root)
        self.s_root.setInitialState(self.s_disconnected)
        self.machine.start()

    def _handle_incoming_action(self, action: LatestAction):
        # We check the active configuration to ensure we are "Online"
        if self.s_connected in self.machine.configuration():
            self.action_ready.emit(action)