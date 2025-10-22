

from typing import Optional
from api.velide import Velide
from config import ApiConfig, TargetSystem
from services.strategies.connectable_strategy import IConnectableStrategy
from PyQt5.QtCore import pyqtSignal, QThreadPool, QObject

from workers.velide_worker import VelideWorker

class DeliverymenRetrieverService(QObject):
    mapping_is_required = pyqtSignal()
    mapping_not_required = pyqtSignal()
    deliverymen_received = pyqtSignal(tuple)
    error = pyqtSignal(str)

    def __init__(self, api_config: ApiConfig, target_system: TargetSystem, strategy: IConnectableStrategy):
        super().__init__()
        self._strategy = strategy
        self._api_config = api_config
        self._target_system = target_system
        self._velide_api: Optional[Velide] = None
        self._waiting_for_token = False

        self._velide_deliverymen: Optional[list] = None
        self._local_deliverymen: Optional[list] = None
        self.thread_pool = QThreadPool.globalInstance()

    def set_access_token(self, access_token: str):
        self._velide_api = Velide(access_token, self._api_config, self._target_system)
        if self._waiting_for_token:
            self.fetch_deliverymen()

    def check_if_mapping_is_required(self):
        if self._strategy.requires_initial_configuration():
            self.mapping_is_required.emit()
        else:
            self.mapping_not_required.emit()

    def _on_receive_velide_deliverymen(self, deliverymen: list):
        self._velide_deliverymen = deliverymen
        if self._local_deliverymen is not None:
            self._emit_deliverymen()
    
    def _on_receive_local_deliverymen(self, deliverymen: list):
        self._local_deliverymen = deliverymen
        if self._velide_deliverymen is not None:
            self._emit_deliverymen()

    def _emit_deliverymen(self):
        self.deliverymen_received.emit((self._velide_deliverymen, self._local_deliverymen))

    def fetch_deliverymen(self):
        if self._velide_api is None:
            self._waiting_for_token = True
            return
        
        velide_deliverymen_retriever = VelideWorker.for_get_deliverymen(self._velide_api)
        velide_deliverymen_retriever.signals.deliverymen_retrieved.connect(self._on_receive_velide_deliverymen)
        velide_deliverymen_retriever.signals.error.connect(self.error)
        self.thread_pool.start(velide_deliverymen_retriever)

        self._strategy.fetch_deliverymen(self._on_receive_local_deliverymen, self.error.emit)