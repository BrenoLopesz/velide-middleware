from __future__ import annotations
from PyQt5.QtCore import QState, QFinalState, pyqtSignal

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models.app_context_model import Services

class DeliverymenMappingWorkflow(QState):
    _on_mapping_stored = pyqtSignal()
    _on_mappings_retrieved = pyqtSignal()
    _no_mappings_found = pyqtSignal()

    def __init__(self, services: 'Services', parent=None):
        super().__init__(parent)
        self.services = services
        self.check_mapping_state = QState(self)
        self.gathering_deliverymen_state = QState(self)  
        self.retrieving_mappings_state = QState(self)
        self.comparing_mappings_state = QState(self)
        # This state shows the mapping UI.
        self.deliverymen_mapping_state = QState(self)
        self.mapping_stored_state = QState(self)
        self.final_state = QFinalState(self)
        self.setInitialState(self.check_mapping_state)

        # TODO: CRITICAL - Add transitions for 'services.sqlite.error_occurred' in 'retrieving_mappings_state' and 'mapping_stored_state' to prevent soft-locks.
#       TODO: Add a transition from 'deliverymen_mapping_state' to 'final_state' for user cancellation (e.g., closing the window - If allowed).

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
        # Gathering Deliverymen → Retrieve Mappings
        self.gathering_deliverymen_state.addTransition(
            self.services.deliverymen_retriever.deliverymen_received,
            self.retrieving_mappings_state
        )
        # Retrieve Mappings → Comparing Mappings
        self.retrieving_mappings_state.addTransition(
            self._on_mappings_retrieved,
            self.comparing_mappings_state
        )
        # Retrieve Mappings → Deliverymen Mappings (not existent yet)
        self.retrieving_mappings_state.addTransition(
            self._no_mappings_found,
            self.deliverymen_mapping_state
        )
        # Comparing Mappings → Deliverymen Mapping (missing deliverymen)
        self.comparing_mappings_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_incomplete,
            self.deliverymen_mapping_state
        )
        # Comparing Mappings → Final (mapping is complete)
        self.comparing_mappings_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_complete,
            self.final_state
        )
        
        # OLD
        # Gathering Deliverymen → Deliverymen Mapping
        # self.gathering_deliverymen_state.addTransition(
        #     self.services.deliverymen_retriever.deliverymen_received,
        #     self.deliverymen_mapping_state
        # )
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

        # All retrieved mappings will trigger 'on_retrieved_mappings'
        self.services.sqlite.all_mappings_found.connect(self.on_retrieved_mappings)
        # Saving mappings will trigger 'on_save_mapping',
        self.services.sqlite.add_many_mappings_result.connect(self.on_save_mapping)

    def on_retrieved_mappings(self, all_mappings_found: list):
        self.setProperty("deliverymen_mappings", all_mappings_found)
        if not all_mappings_found:
            self._no_mappings_found.emit()
        else:
            self._on_mappings_retrieved.emit()

    def on_save_mapping(self, rows_inserted: int):
        self.setProperty("rows_inserted", rows_inserted)
        self._on_mapping_stored.emit()
