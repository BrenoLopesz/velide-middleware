from __future__ import annotations
from PyQt5.QtCore import QState, QFinalState, pyqtSignal

from typing import TYPE_CHECKING
from states.gathering_deliverymen_state import GatheringDeliverymenState
if TYPE_CHECKING:
    from models.app_context_model import Services

class DeliverymenMappingWorkflow(QState):
    _on_mapping_stored = pyqtSignal()

    def __init__(self, services: 'Services', parent=None):
        super().__init__(parent)
        self.services = services
        self.check_mapping_state = QState(self)
        self.gathering_deliverymen_state = QState(self)  
        # This state shows the mapping UI.
        self.deliverymen_mapping_state = QState(self)
        self.mapping_stored_state = QState(self)
        self.final_state = QFinalState(self)
        self.setInitialState(self.check_mapping_state)

        # Checking → Gathering (is required)
        self.check_mapping_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_required,
            self.gathering_deliverymen_state
        )
        # Checking → Finished (not required)
        self.check_mapping_state.addTransition(
            self.services.deliverymen_retriever.mapping_not_required,
            self.final_state
        )
        # Gathering Deliverymen → Deliverymen Mapping
        self.gathering_deliverymen_state.addTransition(
            self.services.deliverymen_retriever.deliverymen_received,
            self.deliverymen_mapping_state
        )
        # Delivery Mapping → Mapping Stored
        self.deliverymen_mapping_state.addTransition(
            self._on_mapping_stored,
            self.mapping_stored_state
        )
        # Mapping Stored → Finished
        self.mapping_stored_state.addTransition(
            self.services.deliverymen_retriever.mapping_finished,
            self.final_state
        )

        # Saving mappings will trigger 'on_save_mapping',
        self.services.sqlite.add_many_mappings_result.connect(self.on_save_mapping)

    def on_save_mapping(self, rows_inserted: int):
        self.setProperty("rows_inserted", rows_inserted)
        self._on_mapping_stored.emit()
