
import logging
from typing import Tuple
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from services.auth_service import AuthService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from PyQt5.QtCore import QObject, pyqtSignal

from services.sqlite_service import SQLiteService
from utils.levenshtein_mapping import generate_levenshtein_mappings
from visual.screens.deliverymen_mapping_screen import DeliverymenMappingScreen

class DeliverymenMappingPresenter(QObject):
    mapping_done = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(
            self, 
            deliverymen_retriever_service: DeliverymenRetrieverService, 
            sqlite_service: SQLiteService,
            auth_service: AuthService,
            mapping_view: DeliverymenMappingScreen
        ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._auth_service = auth_service
        self._deliverymen_retriever_service = deliverymen_retriever_service
        self._sqlite_service = sqlite_service
        self._view = mapping_view

        self._auth_service.access_token.connect(self._deliverymen_retriever_service.set_access_token)
        self._deliverymen_retriever_service.deliverymen_received.connect(self.on_deliverymen_received)
        self._sqlite_service.add_many_mappings_result.connect(self.on_mapping_success)
        self._sqlite_service.error_occurred.connect(self.error)

        mapping_view.save_config.connect(self.validate_mapping)

    def on_deliverymen_received(self, deliverymen: Tuple[DeliverymanResponse, BaseLocalDeliveryman]):
        headers = ["Entregadores Velide", "Entregadores Locais"]
        velide_deliverymen, local_deliverymen = deliverymen
        default_mappings = generate_levenshtein_mappings(velide_deliverymen, local_deliverymen)
        self._view.populate_table(
            source_items=velide_deliverymen, 
            destination_options=local_deliverymen, 
            default_mappings=default_mappings, 
            headers=headers
        )

    def validate_mapping(self):
        mappings = self._view.get_mappings()
        mappings_tuple_list = [(velide_id, local_id) for velide_id, local_id in mappings.items()]
        self._sqlite_service.request_add_many_mappings(mappings_tuple_list)

    def on_mapping_success(self, added: int):
        if added == 0:
            self.logger.warning("Nenhum mapeamento foi salvo.")
        else:
            self.logger.info(f"Mapeamento de {added} entregadores salvo com sucesso.")

        # Proceed anyways
        self.mapping_done.emit()