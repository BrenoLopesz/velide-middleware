from __future__ import annotations
from PyQt5.QtCore import QState

from typing import TYPE_CHECKING
from states.deliverymen_mapping_workflow import DeliverymenMappingWorkflow
if TYPE_CHECKING:
    from models.app_context_model import Services

class LoggedInState(QState):
    def __init__(self, services: 'Services', parent=None):
        super().__init__(parent)
        self.deliverymen_mapping_workflow = DeliverymenMappingWorkflow(services, self)
        self.dashboard_state = QState(self)
        
        # 1. Startup Logic
        self.setInitialState(self.deliverymen_mapping_workflow)

        # 2. Workflow → Dashboard (When finished)
        self.deliverymen_mapping_workflow.addTransition(
            self.deliverymen_mapping_workflow.finished, self.dashboard_state
        )

        # 3. Dashboard → Workflow (Manual Request)
        # When the service emits the signal, we transition BACK to the workflow.
        # Note: The Workflow also hears this signal and sets is_manual_run=True.
        self.dashboard_state.addTransition(
            services.deliverymen_retriever.mapping_requested, 
            self.deliverymen_mapping_workflow
        )