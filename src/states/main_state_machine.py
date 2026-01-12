from __future__ import annotations
from states.websocket_fsm import WebSocketFSM
from PyQt5.QtCore import QStateMachine, QState, pyqtSignal

from states.logged_in_state import LoggedInState
from states.logged_out_state import LoggedOutState
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.app_context_model import Services


class MainStateMachine(QStateMachine):
    _access_token_stored = pyqtSignal()

    def __init__(self, services: "Services"):
        super().__init__()
        self.services = services

        self._create_states()
        self._build_state_machine()
        self._add_transitions()

    def _add_transitions(self):
        self.services.auth.access_token.connect(self._on_access_token_received)
        self.logged_out_state.addTransition(
            self._access_token_stored, self.logged_in_state
        )

    def _on_access_token_received(self, access_token: str):
        self.logged_in_state.setProperty("access_token", access_token)
        self._access_token_stored.emit()

    def _create_states(self):
        """Initializes all QState objects used in the state machine."""

        # --- Main Application States ---
        self.logged_out_state = LoggedOutState(self.services)
        self.logged_in_state = LoggedInState(self.services)

        self.websockets_state = WebSocketFSM(self.services)

        # --- Utility States ---
        self.error_state = QState()
        self.restart_state = QState()

    def _build_state_machine(self):
        """
        Adds states and transitions to the state machine.

        This defines the *flow* of the application 
        (e.g., "from A, on signal X, go to B").
        """
        # Add all created states to the machine
        states = [
            self.logged_out_state,
            self.logged_in_state,
            self.error_state,
            self.restart_state,
        ]
        for state in states:
            self.addState(state)

        self.setup_state_logging(self)
        self.setInitialState(self.logged_out_state)

    def setup_state_logging(self, state: QState, level=0):
        """Recursively setup logging for all states in the hierarchy."""
        indent = "  " * level
        state_name = state.objectName() or state.__class__.__name__

        state.entered.connect(lambda: print(f"{indent}-> ENTERED: {state_name}"))
        state.exited.connect(lambda: print(f"{indent}<- EXITED: {state_name}"))

        for child in state.children():
            if isinstance(child, QState):
                self.setup_state_logging(child, level + 1)
