import logging
from PyQt5.QtCore import QObject, pyqtSignal

from models.velide_delivery_models import Order
from models.velide_websockets_models import ActionType, LatestAction
from repositories.deliveries_repository import DeliveryRepository


class VelideActionHandler(QObject):
    """
    Interprets raw events from the WebSocket and 
    applies changes to the Application State.
    """

    # Emits specific, high-level signals that the Presenter/UI cares about
    delivery_deleted = pyqtSignal(Order)
    delivery_in_route = pyqtSignal(Order, str) # Order, deliverymanId
    delivery_delivered = pyqtSignal(Order)
    route_started = pyqtSignal(str)  # route_id

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
        elif action_type == ActionType.START_ROUTE:
            self._handle_route_started(action)
        elif action_type == ActionType.END_ROUTE:
            self._handle_route_ended(action)

    def _handle_route_started(self, action: LatestAction):
        if not action.route or not action.route.deliveries or not action.deliveryman:
            self.logger.error(
                "Não foi possível gerenciar uma rota iniciada. " \
                "Dados necessários faltando."
            )
            return

        # 1. Get all orders
        order_ids = [delivery.id for delivery in action.route.deliveries]
        orders = [self._repository.get_by_external(order_id) for order_id in order_ids]
        deliveryman = action.deliveryman

        for order in orders:
            if order is not None:
                # 2. Notify UI (Presenter)
                self.delivery_in_route.emit(order, deliveryman.id)

    def _handle_route_ended(self, action: LatestAction):
        if not action.route or not action.route.deliveries:
            self.logger.error(
                "Não foi possível gerenciar uma rota finalizada. " \
                "Dados necessários faltando."
            )
            return

        # 1. Get all orders
        order_ids = [delivery.id for delivery in action.route.deliveries]
        orders = [self._repository.get_by_external(order_id) for order_id in order_ids]

        for order in orders:
            if order is not None:
                # 2. Notify UI (Presenter)
                self.delivery_delivered.emit(order)

    def _handle_deletion(self, action: LatestAction):
        if not action.delivery or not action.delivery.id:
            self.logger.error(
                "Não foi possível gerenciar uma entrega deletada. " \
                "Dados necessários faltando."
            )
            return
        # 1. Translate External ID -> Internal ID
        # Assuming the event carries the External ID
        order = self._repository.get_by_external(action.delivery.id)

        if order:
            # Commented out; Do not remove from repository, if we want
            # to display its status as "CANCELLED".
            # 2. Update State (Repository)
            # self._repository.remove(internal_id)

            # 3. Notify UI (Presenter)
            self.delivery_deleted.emit(order)
        else:
            self.logger.warning(
                "Uma entrega foi deletada no Velide, mas não está "
                "registrada pela integração: %s. "
                "Nenhuma ação será realizada.",
                action.delivery.id
            )
