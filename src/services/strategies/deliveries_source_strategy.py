# in src/services/delivery_strategy.py
from abc import ABC, ABCMeta, abstractmethod
from PyQt5.QtCore import QObject, pyqtSignal
from models.velide_delivery_models import Order

# This new class inherits the "blueprints" from both QObject's metaclass and ABC's metaclass.
class QABCMeta(type(QObject), ABCMeta):
    pass

class IDeliverySourceStrategy(QObject, ABC, metaclass=QABCMeta):
    """
    The interface (contract) for any delivery source.
    Its job is to listen for source-specific data and normalize it into a common Order model.
    """
    # This signal emits the FINAL, NORMALIZED order model
    order_normalized = pyqtSignal(Order)
    # normalization_failed = pyqtSignal(dict, str) # raw_data, error_message

    @abstractmethod
    def start_listening(self):
        """Starts the process of listening for new deliveries from the source."""
        pass

    @abstractmethod
    def stop_listening(self):
        """Stops the listening process."""
        pass