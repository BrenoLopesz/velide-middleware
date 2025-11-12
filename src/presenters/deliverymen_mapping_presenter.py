from __future__ import annotations
import logging
from typing import Tuple
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from services.auth_service import AuthService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from PyQt5.QtCore import QObject, pyqtSignal
from typing import TYPE_CHECKING

from services.sqlite_service import SQLiteService
from states.main_state_machine import MainStateMachine
from utils.levenshtein_mapping import generate_levenshtein_mappings
from visual.main_view import MainView
from visual.screens.deliverymen_mapping_screen import DeliverymenMappingScreen

if TYPE_CHECKING:
    from models.app_context_model import Services

class DeliverymenMappingPresenter(QObject):
    mapping_done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(
            self, 
            view: MainView,
            services: 'Services',
            machine: MainStateMachine
        ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._view = view
        self._services = services
        self._machine = machine
        

        deliverymen_mapping_workflow = self._machine.logged_in_state.deliverymen_mapping_workflow
        # Checks if mapping is required
        deliverymen_mapping_workflow.check_mapping_state.entered.connect(
            self._services.deliverymen_retriever.check_if_mapping_is_required
        )
        # Gathers deliverymen
        deliverymen_mapping_workflow.gathering_deliverymen_state.entered.connect(
            self._on_start_gathering_deliverymen
        )
        # Retrieving mappings
        deliverymen_mapping_workflow.retrieving_mappings_state.entered.connect(
            self._services.sqlite.request_get_all_mappings
        )
        # Comparing mappings
        deliverymen_mapping_workflow.comparing_mappings_state.entered.connect(
            self._on_mappings_retrieved
        )

        # Display table when deliverymen received
        deliverymen_mapping_workflow.deliverymen_mapping_state.entered.connect(self.on_deliverymen_received)
        # On clicking to save
        self._view.deliverymen_mapping_screen.save_config.connect(self.validate_mapping)
        # After saving mappings
        deliverymen_mapping_workflow.mapping_stored_state.entered.connect(self.on_mapping_success)

        # TODO: Remove this last 'anti-pattern'
        self._services.sqlite.error_occurred.connect(self.error)

    def _on_start_gathering_deliverymen(self):
        # Updates Deliverymen Retriever service access token
        self._services.deliverymen_retriever.set_access_token(
            self._machine.logged_in_state.property("access_token")
        )
        # Then fetch deliverymen
        self._services.deliverymen_retriever.fetch_deliverymen()

    def _on_mappings_retrieved(self):
        mappings = self._machine.logged_in_state.deliverymen_mapping_workflow.property("deliverymen_mappings")
        velide_deliverymen, local_deliverymen = self._services.deliverymen_retriever.get_deliverymen()
        self._services.deliverymen_retriever.check_if_mapping_is_complete(
            mappings,
            velide_deliverymen,
            local_deliverymen
        )

    def on_deliverymen_received(self):
        # 1. Populate table
        headers = ["Entregadores Velide", "Entregadores Locais"]
        velide_deliverymen, local_deliverymen = self._services.deliverymen_retriever.get_deliverymen()
        default_mappings = generate_levenshtein_mappings(velide_deliverymen, local_deliverymen)
        self._view.deliverymen_mapping_screen.populate_table(
            source_items=velide_deliverymen, 
            destination_options=local_deliverymen, 
            default_mappings=default_mappings, 
            headers=headers
        )
        # 2. Change view
        self._view.deliverymen_mapping_screen.set_screen(1)

    def validate_mapping(self):
        # 1. Get the {velide_id: local_name} mappings from the view
        name_mappings = self._view.deliverymen_mapping_screen.get_mappings()

        # 2. Get the full list of local deliverymen from the service
        _, local_deliverymen = self._services.deliverymen_retriever.get_deliverymen()

        # 3. Create a {local_name: local_id} lookup dictionary
        #    This is the crucial conversion step.
        name_to_id_map = {dm.name: dm.id for dm in local_deliverymen}

        # 4. Build the final {velide_id: local_id} list
        mappings_tuple_list = []
        for velide_id, local_name in name_mappings.items():
            # Find the local_id corresponding to the selected local_name
            local_id = name_to_id_map.get(local_name)
            
            # Only add to the list if a valid mapping was found
            if local_id:
                mappings_tuple_list.append((velide_id, local_id))
            else:
                self.logger.warning(
                    f"Ignorando o mapeamento para {velide_id}: "
                    f"o nome '{local_name}' não foi encontrado."
                )

        # 5. Send the correct ID-to-ID list to the database
        self._services.sqlite.request_add_many_mappings(mappings_tuple_list)

    def on_mapping_success(self):
        deliverymen_mapping_workflow = self._machine.logged_in_state.deliverymen_mapping_workflow
        added = deliverymen_mapping_workflow.property("rows_inserted")
        if added == 0:
            self.logger.warning("Nenhum mapeamento foi alterado.")
        else:
            self.logger.info(f"Mapeamento de {added} entregadores salvo com sucesso.")

        # Proceed anyways
        self._services.deliverymen_retriever.mark_mapping_as_finished()