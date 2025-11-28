# in src/services/delivery_strategy.py
from abc import ABC, ABCMeta, abstractmethod
from typing import Optional, Tuple
from PyQt5.QtCore import QObject, pyqtSignal
from models.velide_delivery_models import Order

# This new class inherits the "blueprints" from both QObject's metaclass and ABC's metaclass.
class QABCMeta(type(QObject), ABCMeta):
    pass

class IConnectableStrategy(QObject, ABC, metaclass=QABCMeta):
    """
    The interface (contract) for any delivery source.
    Its job is to listen for source-specific data and normalize it into a common Order model.
    """
    # This signal emits the FINAL, NORMALIZED order model
    order_normalized = pyqtSignal(Order)
    order_restored = pyqtSignal(Order)
    order_cancelled = pyqtSignal(str, object) # Internal ID, (optional) external ID
    # normalization_failed = pyqtSignal(dict, str) # raw_data, error_message

    @abstractmethod
    def start_listening(self):
        """Starts the process of listening for new deliveries from the source."""
        pass

    @abstractmethod
    def stop_listening(self):
        """Stops the listening process."""
        pass

    @abstractmethod
    def requires_initial_configuration(self) -> bool:
        """Returns True if the config screen must be shown first."""
        pass

    @abstractmethod
    def fetch_deliverymen(self, success, error) -> list:
        """Returns the local registered deliverymen on the software."""
        pass

    @abstractmethod
    def on_delivery_added(self, internal_id: str, external_id: str):
        """Optional callback function to receive deliveries added notifications."""
        pass

    @abstractmethod
    def on_delivery_failed(self, internal_id: Optional[float]):
        """Optional callback function to receive deliveries added notifications."""
        pass