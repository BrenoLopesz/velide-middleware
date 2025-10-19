import logging

from PyQt5.QtCore import pyqtSignal
from config import ApiConfig, FarmaxConfig
from models.velide_delivery_models import Order
from services.strategies.deliveries_source_strategy import IDeliverySourceStrategy

class FarmaxStrategy(IDeliverySourceStrategy):
    # Signals for the Presenter
    order_normalized = pyqtSignal(Order)

    def __init__(self, api_config: ApiConfig, farmax_config: FarmaxConfig, sqlite_database: str):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._api_config = api_config
        self._farmax_config = farmax_config 

    