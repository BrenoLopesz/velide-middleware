from PyQt5.QtCore import QStateMachine, QState

from states.gathering_deliverymen_state import GatheringDeliverymenState


class MainStateMachine(QStateMachine):
    def __init__(self):
        super().__init__()
        self._create_states()
        self._build_state_machine()

    def _create_states(self):
        """Initializes all QState objects used in the state machine."""
        
        # --- Main Application States ---
        self.initial_state = QState()
        self.device_flow_state = QState()
        self.dashboard_state = QState()

        # --- Mapping & Setup States ---
        
        # This state checks if mapping is needed and branches.
        self.check_mapping_state = QState()

        self.gathering_deliverymen_state = GatheringDeliverymenState()
        
        # This state shows the mapping UI.
        self.deliverymen_mapping_state = QState()
       
        # --- Utility States ---
        self.error_state = QState()
        self.restart_state = QState()

    def _build_state_machine(self):
        """
        Adds states and transitions to the state machine.
        
        This defines the *flow* of the application (e.g., "from A, on signal X, go to B").
        """
        # Add all created states to the machine
        states = [
            self.initial_state,
            self.device_flow_state,
            self.dashboard_state,
            self.check_mapping_state,
            self.gathering_deliverymen_state,
            self.deliverymen_mapping_state,
            self.error_state,
            self.restart_state
        ]
        for state in states:
            self.addState(state)
            
        self.setInitialState(self.initial_state)
    