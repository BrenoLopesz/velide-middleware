from __future__ import annotations
import logging
from PyQt5.QtCore import QObject, pyqtSignal
from typing import TYPE_CHECKING

from states.main_state_machine import MainStateMachine
from utils.levenshtein_mapping import generate_levenshtein_mappings
from visual.main_view import MainView

if TYPE_CHECKING:
    from models.app_context_model import Services


class DeliverymenMappingPresenter(QObject):
    mapping_done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, view: MainView, services: "Services", machine: MainStateMachine):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._view = view
        self._services = services
        self._machine = machine

        deliverymen_mapping_workflow = (
            self._machine.logged_in_state.deliverymen_mapping_workflow
        )
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
        deliverymen_mapping_workflow.deliverymen_mapping_state.entered.connect(
            self.on_deliverymen_received
        )

        # On clicking to save
        self._view.deliverymen_mapping_screen.save_config.connect(self.validate_mapping)

        self._view.deliverymen_mapping_screen.cancel_config.connect(
            self._services.deliverymen_retriever.request_deliverymen_mapping_exit
        )

        # After saving mappings
        deliverymen_mapping_workflow.mapping_stored_state.entered.connect(
            self.on_mapping_success
        )

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
        mappings = self._machine.logged_in_state.deliverymen_mapping_workflow.property(
            "deliverymen_mappings"
        )
        velide_deliverymen, local_deliverymen = (
            self._services.deliverymen_retriever.get_deliverymen()
        )
        self._services.deliverymen_retriever.check_if_mapping_is_complete(
            mappings, velide_deliverymen, local_deliverymen
        )

    def on_deliverymen_received(self):
        headers = ["Entregadores Velide", "Entregadores Locais"]


        # 1. Retrieve the saved mappings from the state machine
        mapping_workflow = self._machine.logged_in_state.deliverymen_mapping_workflow
        saved_mappings_list = mapping_workflow.property(
             "deliverymen_mappings"
        )
        # Result: {velide_id: local_id}
        saved_map_dict = {
            m[0]: m[1] for m in saved_mappings_list
        } if saved_mappings_list else {}

        # 2. Get the deliverymen lists
        velide_deliverymen, local_deliverymen = (
            self._services.deliverymen_retriever.get_deliverymen()
        )

        # 3. Generate the auto-guesses
        # Returns Dict[velide_id, local_name]
        auto_mappings = generate_levenshtein_mappings(
            velide_deliverymen, local_deliverymen
        )

        # 4. MERGE: Overwrite auto-guesses with saved data
        final_mappings = auto_mappings.copy()
        
        for v_man in velide_deliverymen:
            # Check if we have a saved decision for this Velide ID
            if v_man.id in saved_map_dict:
                saved_local_id = saved_map_dict[v_man.id]
                
                # Find the local deliveryman object by the SAVED ID
                found_local = next(
                    (local_deliveryman for local_deliveryman 
                     in local_deliverymen 
                     if local_deliveryman.id == saved_local_id), 
                    None
                )
                
                if found_local:
                    # FIX: Use 'v_man.id' (key) and 'found_local.name' (value)
                    # to match the format returned by generate_levenshtein_mappings
                    final_mappings[v_man.id] = found_local.name

        # 5. Populate table with the MERGED dictionary
        self._view.deliverymen_mapping_screen.populate_table(
            source_items=velide_deliverymen,
            destination_options=local_deliverymen,
            default_mappings=final_mappings, 
            headers=headers,
        )
        
        # 6. Change view
        self._view.deliverymen_mapping_screen.set_screen(1)

    def validate_mapping(self):
        # TODO: Ensure 'request_add_many_mappings' performs
        # an UPSERT (INSERT OR REPLACE).
        # Standard INSERT will crash on existing IDs.

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
        deliverymen_mapping_workflow = (
            self._machine.logged_in_state.deliverymen_mapping_workflow
        )
        added = deliverymen_mapping_workflow.property("rows_inserted")
        if added == 0:
            self.logger.warning("Nenhum mapeamento foi alterado.")
        else:
            self.logger.info(f"Mapeamento de {added} entregadores salvo com sucesso.")

        # Proceed anyways
        self._services.deliverymen_retriever.mark_mapping_as_finished()
