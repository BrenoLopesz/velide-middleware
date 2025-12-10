import logging
from PyQt5.QtCore import QObject, pyqtSignal

from models.velide_delivery_models import Order
from models.velide_websockets_models import ActionType, LatestAction
from repositories.deliveries_repository import DeliveryRepository

class VelideActionHandler(QObject):
    """
    Interprets raw events from the WebSocket and applies changes to the Application State.
    """
    
    # Emits specific, high-level signals that the Presenter/UI cares about
    delivery_updated = pyqtSignal(str) # internal_id
    delivery_deleted = pyqtSignal(Order)
    route_started = pyqtSignal(str)    # route_id

    def __init__(self, repository: DeliveryRepository):
        super().__init__()
        self._repository = repository
        self.logger = logging.getLogger(__name__)

    def handle_action(self, action: LatestAction):
        """
        Slot connected to VelideWebsocketsService.action_received
        """
        action_type = action.action_type
        self.logger.debug(f"Lidando com ação do Websocket: {action_type}")

        if action_type == ActionType.DELETE_DELIVERY:
            self._handle_deletion(action)
    
    def _handle_deletion(self, action: LatestAction):
        # 1. Translate External ID -> Internal ID
        # Assuming the event carries the External ID
        order = self._repository.get_by_external(action.delivery.id)
        
        if order:
            internal_id = order.internal_id
            
            # Commented out; Do not remove from repository, if we want
            # to display its status as "CANCELLED".
            # 2. Update State (Repository)
            # self._repository.remove(internal_id)
            
            # 3. Notify UI (Presenter)
            self.delivery_deleted.emit(order)
        else:
            self.logger.warning(f"Uma entrega foi deletada no Velide, mas não está registrada pela integração: {action.delivery.id}. Nenhuma ação será realizada.")