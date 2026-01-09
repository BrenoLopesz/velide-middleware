import logging
from typing import Dict, List, Optional, Tuple
from api.velide import Velide
from config import ApiConfig, TargetSystem
from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from services.strategies.connectable_strategy import ERPFeature, IConnectableStrategy
from PyQt5.QtCore import pyqtSignal, QThreadPool, QObject

from workers.velide_worker import VelideWorker


class DeliverymenRetrieverService(QObject):
    mapping_is_required = pyqtSignal()
    mapping_not_required = pyqtSignal()
    deliverymen_received = pyqtSignal(tuple)
    mapping_is_complete = pyqtSignal()
    mapping_is_incomplete = pyqtSignal()
    mapping_finished = pyqtSignal()
    mapping_requested = pyqtSignal()
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

        self._mapping_ids: Dict[str, str] = {}

    def set_access_token(self, access_token: str) -> None:
        self._velide_api = Velide(access_token, self._api_config, self._target_system)
        if self._waiting_for_token:
            self.fetch_deliverymen()

    def set_strategy(self, strategy: IConnectableStrategy):
        """Sets the active deliverymen source strategy."""
        self._strategy = strategy

    def get_deliverymen(self) -> Tuple[Optional[list], Optional[list]]:
        return (self._velide_deliverymen, self._local_deliverymen)

    def get_deliveryman_by_external_id(
        self, external_id: str
    ) -> Optional[BaseLocalDeliveryman]:
        if not self._local_deliverymen:
            return None
        
        internal_id = next(
            (
                mapping[1]
                for mapping in self._mapping_ids
                if mapping[0] == external_id
            ),
            None,
        )
        local_deliverymen = self._local_deliverymen

        if internal_id is None or local_deliverymen is None:
            return None
        
        return next(
            (
                local_deliveryman
                for local_deliveryman in local_deliverymen
                if local_deliveryman.id == internal_id
            ),
            None,
        )

    def check_if_mapping_is_required(self) -> None:
        if not self._strategy:
            self.logger.error(
                "Não há nenhum software conectado para verificar "
                "se é necessário buscar os entregadores."
            )
            return

        if ERPFeature.REQUIRES_INITIAL_CONFIG in self._strategy.capabilities:
            self.mapping_is_required.emit()
        else:
            self.mapping_not_required.emit()

    def request_deliverymen_mapping_screen(self) -> None:
        """Called by the Presenter when user clicks the config button."""
        self.mapping_requested.emit()

    def mark_mapping_as_finished(self) -> None:
        self.mapping_finished.emit()

    def check_if_mapping_is_complete(
        self,
        mappings: List[Tuple[str, str]],
        velide_deliverymen: List[DeliverymanResponse],
        local_deliverymen: List[BaseLocalDeliveryman],
    ) -> None:
        # 1. Create a set of valid local IDs for fast lookup
        valid_local_ids = {d.id for d in local_deliverymen}

        # 2. Optimization: Convert the list of tuples to a Dict immediately.
        #    This solves the original bug and makes lookups instant (O(1)).
        mapped_dict = dict(mappings)

        for velide_deliveryman in velide_deliverymen:
            # Check 1: Does a mapping exist for this Velide ID?
            # Using .get() is faster and cleaner than looping through a list
            mapped_local_id = mapped_dict.get(velide_deliveryman.id)

            if mapped_local_id is None:
                # Case 1: No mapping exists for this user
                self.mapping_is_incomplete.emit()
                return

            # Check 2: Is the mapped local ID actually valid?
            if mapped_local_id not in valid_local_ids:
                # Case 2: Mapping exists, but the local_id is invalid/stale
                self.mapping_is_incomplete.emit()
                return

        # 3. Success: All checks passed.
        #    We assign the dict we created at the start to self._mapping_ids
        self._mapping_ids = mapped_dict
        self.mapping_is_complete.emit()

    def _on_receive_velide_deliverymen(self, deliverymen: list) -> None:
        self._velide_deliverymen = deliverymen
        if self._local_deliverymen is not None:
            self._emit_deliverymen()

    def _on_receive_local_deliverymen(
        self, deliverymen: List[BaseLocalDeliveryman]
    ) -> None:
        self._local_deliverymen = deliverymen
        if self._velide_deliverymen is not None:
            self._emit_deliverymen()

    def _emit_deliverymen(self) -> None:
        self.deliverymen_received.emit(
            (self._velide_deliverymen, self._local_deliverymen)
        )

    def fetch_deliverymen(self) -> None:
        if self._velide_api is None:
            self._waiting_for_token = True
            return

        if not self._strategy:
            self.logger.error(
                "Não há nenhum software conectado para buscar os entregadores!"
            )
            return

        velide_deliverymen_retriever = VelideWorker.for_get_deliverymen(
            self._velide_api
        )
        velide_deliverymen_retriever.signals.deliverymen_retrieved.connect(
            self._on_receive_velide_deliverymen
        )
        velide_deliverymen_retriever.signals.error.connect(self.error.emit)
        self.thread_pool.start(velide_deliverymen_retriever)

        self._strategy.fetch_deliverymen(
            self._on_receive_local_deliverymen, self.error.emit
        )
