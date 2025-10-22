import logging

from PyQt5.QtCore import pyqtSignal, QThreadPool
from config import ApiConfig, FarmaxConfig
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
from models.velide_delivery_models import Order
from services.strategies.connectable_strategy import IConnectableStrategy

class FarmaxStrategy(IConnectableStrategy):
    # Signals for the Presenter
    order_normalized = pyqtSignal(Order)

    def __init__(self, farmax_config: FarmaxConfig, farmax_repository: FarmaxRepository):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.config = farmax_config
        self._farmax = farmax_repository
        self._thread_pool = QThreadPool.globalInstance() 

    def start_listening(self):
        raise NotImplementedError
    
    def stop_listening(self):
        raise NotImplementedError
    
    def requires_initial_configuration(self):
        return True

    def fetch_deliverymen(self, success, error):
        worker = FarmaxWorker(self._farmax)
        worker.signals.success.connect(success)
        worker.signals.error.connect(error)
        self._thread_pool.start(worker)


    