

import logging
from typing import List, Optional, Tuple
from api.velide import Velide
from config import ApiConfig, TargetSystem
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from services.strategies.connectable_strategy import IConnectableStrategy
from PyQt5.QtCore import pyqtSignal, QThreadPool, QObject

from workers.velide_worker import VelideWorker

class DeliverymenRetrieverService(QObject):
    mapping_is_required = pyqtSignal()
    mapping_not_required = pyqtSignal()
    deliverymen_received = pyqtSignal(tuple)
    mapping_is_complete = pyqtSignal()
    mapping_is_incomplete = pyqtSignal()
    mapping_finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, api_config: ApiConfig, target_system: TargetSystem):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        
        self._api_config = api_config
        self._target_system = target_system
        self._strategy: Optional[IConnectableStrategy] = None
        self._velide_api: Optional[Velide] = None
        self._waiting_for_token = False

        self._velide_deliverymen: Optional[list] = None
        self._local_deliverymen: Optional[List[BaseLocalDeliveryman]] = None
        self.thread_pool = QThreadPool.globalInstance()

    def set_access_token(self, access_token: str) -> None:
        self._velide_api = Velide(access_token, self._api_config, self._target_system)
        if self._waiting_for_token:
            self.fetch_deliverymen()

    def set_strategy(self, strategy: IConnectableStrategy):
        """Sets the active deliverymen source strategy."""
        self._strategy = strategy

    def get_deliverymen(self) -> Tuple[Optional[list], Optional[list]]:
        return (self._velide_deliverymen, self._local_deliverymen)
    
    def get_deliveryman_by_external_id(self, external_id: str) -> Optional[BaseLocalDeliveryman]:
        if not self._local_deliverymen:
            return None
        return next((deliveryman for deliveryman in self._local_deliverymen if deliveryman.id == external_id), None)

    def check_if_mapping_is_required(self) -> None:
        if not self._strategy:
            self.logger.error("Não há nenhum software conectado para verificar se é necessário buscar os entregadores.")
            return
        
        if self._strategy.requires_initial_configuration():
            self.mapping_is_required.emit()
        else:
            self.mapping_not_required.emit()

    def mark_mapping_as_finished(self) -> None:
        self.mapping_finished.emit()

    def check_if_mapping_is_complete(
        self, 
        mappings: list, 
        velide_deliverymen: List[DeliverymanResponse], 
        local_deliverymen: List[BaseLocalDeliveryman]
    ) -> None:
        # Create a set of valid local IDs for fast lookup
        valid_local_ids = {d.id for d in local_deliverymen}

        for velide_deliveryman in velide_deliverymen:
            # Find the full mapping tuple (velide_id, local_id)
            mapping_tuple = next((mapping for mapping in mappings if mapping[0] == velide_deliveryman.id), None)
            
            if mapping_tuple is None:
                # Case 1: No mapping exists at all for this Velide user
                self.mapping_is_incomplete.emit()
                return

            mapped_local_id = mapping_tuple[1] # Get the local_id from the mapping
            if mapped_local_id not in valid_local_ids:
                # Case 2: Mapping exists, but the local_id is stale/invalid
                self.mapping_is_incomplete.emit()
                return
        
        # All Velide users have a *valid* and *current* mapping
        self.mapping_is_complete.emit()

    def _on_receive_velide_deliverymen(self, deliverymen: list) -> None:
        self._velide_deliverymen = deliverymen
        if self._local_deliverymen is not None:
            self._emit_deliverymen()
    
    def _on_receive_local_deliverymen(self, deliverymen: List[BaseLocalDeliveryman]) -> None:
        self._local_deliverymen = deliverymen
        if self._velide_deliverymen is not None:
            self._emit_deliverymen()

    def _emit_deliverymen(self) -> None:
        self.deliverymen_received.emit((self._velide_deliverymen, self._local_deliverymen))

    def fetch_deliverymen(self) -> None:
        if self._velide_api is None:
            self._waiting_for_token = True
            return
        
        if not self._strategy:
            self.logger.error("Não há nenhum software conectado para buscar os entregadores!")
            return
        
        velide_deliverymen_retriever = VelideWorker.for_get_deliverymen(self._velide_api)
        velide_deliverymen_retriever.signals.deliverymen_retrieved.connect(self._on_receive_velide_deliverymen)
        velide_deliverymen_retriever.signals.error.connect(self.error)
        self.thread_pool.start(velide_deliverymen_retriever)

        self._strategy.fetch_deliverymen(self._on_receive_local_deliverymen, self.error.emit)