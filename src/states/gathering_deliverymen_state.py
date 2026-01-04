# states/gathering_deliverymen_state.py
from __future__ import annotations
from typing import TYPE_CHECKING
from PyQt5.QtCore import QState, QFinalState, pyqtSignal

if TYPE_CHECKING:
    from models.app_context_model import Services


class GatheringDeliverymenState(QState):
    """
    A parallel state that waits for two data sources to complete gathering:
    - Local deliverymen from the local system
    - Velide deliverymen from the remote API

    Once both signals are received, this state transitions to finished.

    Usage:
        state = GatheringDeliverymenState()
        service.local_received.connect(state.on_local_received)
        service.velide_received.connect(state.on_velide_received)
        machine.addState(state)
    """

    # Signals that the state emits when data is received
    local_received = pyqtSignal()
    velide_received = pyqtSignal()

    def __init__(self, services: "Services", parent=None):
        super().__init__(QState.ChildMode.ParallelStates, parent)
        self._services = services
        self._local_done = False
        self._velide_done = False

        self._setup_sub_states()
        self._connect_internal_transitions()

    def _setup_sub_states(self):
        """Creates the two parallel sub-state machines."""

        # Local deliverymen gathering flow
        self.local_state = QState(self)
        self.local_waiting = QState(self.local_state)
        self.local_finished = QFinalState(self.local_state)
        self.local_state.setInitialState(self.local_waiting)

        # Velide deliverymen gathering flow
        self.velide_state = QState(self)
        self.velide_waiting = QState(self.velide_state)
        self.velide_finished = QFinalState(self.velide_state)
        self.velide_state.setInitialState(self.velide_waiting)

    def _connect_internal_transitions(self):
        """Sets up transitions within the parallel states."""
        # When local data arrives, move to finished state
        self.local_waiting.addTransition(self.local_received, self.local_finished)

        # When velide data arrives, move to finished state
        self.velide_waiting.addTransition(self.velide_received, self.velide_finished)

    def on_local_received(self):
        """Slot called when local deliverymen data is received."""
        self.local_received.emit()
        self._local_done = True
        if self._velide_done is True:
            self.finished.emit()

    def on_velide_received(self):
        """Slot called when velide deliverymen data is received."""
        self.velide_received.emit()
        self._velide_done = True
        if self._local_done is True:
            self.finished.emit()
