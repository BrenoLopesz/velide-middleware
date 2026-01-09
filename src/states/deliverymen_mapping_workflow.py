from __future__ import annotations
from PyQt5.QtCore import QState, QFinalState, pyqtSignal

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.app_context_model import Services


class DeliverymenMappingWorkflow(QState):
    # Public signals
    _on_mapping_stored = pyqtSignal()
    _on_mappings_retrieved = pyqtSignal()
    _no_mappings_found = pyqtSignal()
    
    # Internal Signals (for the decision state)
    _force_ui_open = pyqtSignal()
    _skip_ui_finish = pyqtSignal()

    def __init__(self, services: "Services", parent=None):
        super().__init__(parent)
        self.services = services
        
        # --- 1. The Manual Flag ---
        self.is_manual_run = False 

        # --- 2. Connect Service to Flag ---
        # This is the key "Philosophy" part. 
        # When service emits 'requested', we flag this run as manual.
        self.services.deliverymen_retriever.mapping_requested.connect(
            self._enable_manual_mode
        )

        # --- States ---
        self.check_mapping_state = QState(self)
        self.gathering_deliverymen_state = QState(self)
        self.retrieving_mappings_state = QState(self)
        self.comparing_mappings_state = QState(self)
        
        # The Decision State (Route logic)
        self.completion_decision_state = QState(self)
        
        self.deliverymen_mapping_state = QState(self)  # The UI Screen
        self.mapping_stored_state = QState(self)
        self.final_state = QFinalState(self)

        self.setInitialState(self.check_mapping_state)

         # TODO: CRITICAL - Add transitions for 'services.sqlite.error_occurred' 
        #   in 'retrieving_mappings_state' and 'mapping_stored_state' 
        #   to prevent soft-locks.
        # TODO: Add a transition from 'deliverymen_mapping_state' to 
        #   'final_state' for user cancellation 
        #   (e.g., closing the window - If allowed).

        # --- Transitions ---

        # 1. Initial Check → Gather (is required)
        self.check_mapping_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_required,
            self.gathering_deliverymen_state,
        )
        # Initial Check → Final (not required)
        self.check_mapping_state.addTransition(
            self.services.deliverymen_retriever.mapping_not_required, 
            self.final_state
        )

        # Gathering Deliverymen → Retrieve Mappings
        self.gathering_deliverymen_state.addTransition(
            self.services.deliverymen_retriever.deliverymen_received,
            self.retrieving_mappings_state,
        )

        # Retrieve → Comparing Mappings
        self.retrieving_mappings_state.addTransition(
            self._on_mappings_retrieved, self.comparing_mappings_state
        )
        # If absolutely no mappings found, go to UI immediately
        self.retrieving_mappings_state.addTransition(
            self._no_mappings_found, self.deliverymen_mapping_state
        )

        # Compare → UI (If incomplete, always show UI)
        self.comparing_mappings_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_incomplete,
            self.deliverymen_mapping_state,
        )
        
        # 5. Compare → Decision (If complete)
        # We DO NOT go to final yet. We go to decision state.
        self.comparing_mappings_state.addTransition(
            self.services.deliverymen_retriever.mapping_is_complete, 
            self.completion_decision_state
        )

        # 6. Decision Logic
        # On entry, we decide where to go
        self.completion_decision_state.entered.connect(self._decide_next_step)
        
        # If manual → Force UI
        self.completion_decision_state.addTransition(
            self._force_ui_open, self.deliverymen_mapping_state
        )
        # If auto → Finish
        self.completion_decision_state.addTransition(
            self._skip_ui_finish, self.final_state
        )

        # UI → Stored → Finished
        self.deliverymen_mapping_state.addTransition(
            self._on_mapping_stored, self.mapping_stored_state
        )
        # Stored → Finished
        self.mapping_stored_state.addTransition(
            self.services.deliverymen_retriever.mapping_finished, self.final_state
        )

        # --- Connections ---
        # All retrieved mappings will trigger 'on_retrieved_mappings'
        self.services.sqlite.all_mappings_found.connect(self.on_retrieved_mappings)
        # Saving mappings will trigger 'on_save_mapping',
        self.services.sqlite.add_many_mappings_result.connect(self.on_save_mapping)
        
        # Reset the flag when the workflow finishes entirely
        self.finished.connect(self._reset_manual_mode)

    # --- Logic ---

    def _enable_manual_mode(self):
        """Slot called by Service signal."""
        self.is_manual_run = True

    def _reset_manual_mode(self):
        """Reset after workflow finishes so next auto-run works correctly."""
        self.is_manual_run = False

    def _decide_next_step(self):
        """The brain of the Decision State."""
        if self.is_manual_run:
            self._force_ui_open.emit()
        else:
            self._skip_ui_finish.emit()

    def on_retrieved_mappings(self, all_mappings_found: list):
        self.setProperty("deliverymen_mappings", all_mappings_found)
        if not all_mappings_found:
            self._no_mappings_found.emit()
        else:
            self._on_mappings_retrieved.emit()

    def on_save_mapping(self, rows_inserted: int):
        self.setProperty("rows_inserted", rows_inserted)
        self._on_mapping_stored.emit()
