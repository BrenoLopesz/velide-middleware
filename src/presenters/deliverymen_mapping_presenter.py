
from typing import Tuple
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from services.auth_service import AuthService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from PyQt5.QtCore import QObject, pyqtSignal

from utils.levenshtein_mapping import generate_levenshtein_mappings
from visual.screens.deliverymen_mapping_screen import DeliverymenMappingScreen

class DeliverymenMappingPresenter(QObject):
    mapping_done = pyqtSignal()

    def __init__(
            self, 
            deliverymen_retriever_service: DeliverymenRetrieverService, 
            auth_service: AuthService,
            mapping_view: DeliverymenMappingScreen
        ):
        super().__init__()
        self._auth_service = auth_service
        self._service = deliverymen_retriever_service
        self._view = mapping_view

        self._auth_service.access_token.connect(self._service.set_access_token)
        self._service.deliverymen_received.connect(self.on_deliverymen_received)

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
        # TODO: Validate
        print(self._view.get_mappings())
        self.mapping_done.emit()