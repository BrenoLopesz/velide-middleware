from __future__ import annotations
import logging
from PyQt5.QtCore import QObject, QStateMachine, QState, pyqtSignal
from models.velide_websockets_models import LatestAction
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models.app_context_model import Services   


class WebSocketFSM(QObject):
    """
    Finite State Machine for the Velide WebSocket.
    
    Roles:
    1. Lifecycle Manager: Tracks Offline -> Connecting -> Online states.
    2. Data Facade: acts as a gatekeeper, only forwarding data to the 
       Presenter when the system is in the 'Online' state.
    """

    # --- Signals for the Presenter ---
    # The Presenter listens to THIS, not the Service.
    state_changed = pyqtSignal(str)          # Payloads: "Offline", "Connecting", "Online"
    action_ready = pyqtSignal(LatestAction)  # Emitted only when valid data arrives in Online state
    
    def __init__(self, services: 'Services'):
        """
        Args:
            service: Instance of VelideWebsocketsService
        """
        super().__init__()
        self.service = services.websockets
        self.machine = QStateMachine()
        self.logger = logging.getLogger(__name__)

        # --- 1. Define States ---
        self.s_offline = QState()
        self.s_connecting = QState()
        self.s_online = QState()

        # --- 2. Configure Transitions ( The Wiring ) ---

        # Trigger: Service starts
        # Action: Transition Offline -> Connecting
        self.s_offline.addTransition(self.service.started, self.s_connecting)

        # Trigger: Service successfully connects (Handshake complete)
        # Action: Transition Connecting -> Online
        self.s_connecting.addTransition(self.service.connected, self.s_online)

        # Trigger: Service fails to connect (Error during handshake)
        # Action: Transition Connecting -> Offline (could also go to an Error state)
        self.s_connecting.addTransition(self.service.error_occurred, self.s_offline)
        # Also handle explicit disconnects during connection phase
        self.s_connecting.addTransition(self.service.disconnected, self.s_offline)

        # Trigger: Connection lost while Online
        # Action: Transition Online -> Offline
        self.s_online.addTransition(self.service.disconnected, self.s_offline)
        
        # Trigger: Error while Online (optional, depending on if error implies disconnect)
        self.s_online.addTransition(self.service.error_occurred, self.s_offline)

        # --- 3. Setup State Entry Signals (Feedback for UI) ---
        
        # When entering a state, emit the string name to the Presenter
        self.s_offline.entered.connect(lambda: self.state_changed.emit("Offline"))
        self.s_connecting.entered.connect(lambda: self.state_changed.emit("Connecting"))
        self.s_online.entered.connect(lambda: self.state_changed.emit("Online"))

        # --- 4. Data Facade Logic (The Gatekeeper) ---
        
        # Connect the service's raw data signal to our internal handler
        self.service.action_received.connect(self._handle_incoming_action)

        # --- 5. Start the Machine ---
        self.machine.addState(self.s_offline)
        self.machine.addState(self.s_connecting)
        self.machine.addState(self.s_online)
        
        self.machine.setInitialState(self.s_offline)
        self.machine.start()

    def _handle_incoming_action(self, action: LatestAction):
        """
        Internal slot to handle incoming data.
        It checks the current state of the machine before forwarding.
        """
        self.logger.debug(f"Package recebido: {action}")
        # We query the machine: "Is the s_online state currently active?"
        # self.machine.configuration() returns the set of currently active states.
        if self.s_online in self.machine.configuration():
            # logic: Pass the data through to the Presenter
            self.action_ready.emit(action)
        else:
            # logic: Drop the packet.
            # We are likely connecting or disconnecting, so this data 
            # might be stale or irrelevant.
            # You could add logging here: print(f"Dropped action {action.offset} - State not Online")
            pass