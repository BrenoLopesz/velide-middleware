from __future__ import annotations
import logging
from PyQt5.QtCore import QObject
from pydantic import ValidationError
from models.delivery_table_model import DeliveryRowModel, DeliveryRowStatus, DeliveryTableModel
from models.velide_delivery_models import Order
from services.auth_service import AuthService
from services.deliveries_service import DeliveriesService
from states.main_state_machine import MainStateMachine
from visual.screens.dashboard_screen import DashboardScreen
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models.app_context_model import Services

class DashboardPresenter(QObject):
    def __init__(self, view: DashboardScreen, services: 'Services', state_machine: MainStateMachine):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._view = view
        self._services = services
        self._state_machine = state_machine

        self._state_machine.logged_in_state.entered.connect(self.on_authenticate)

        self._connect_signals()

    def start(self):
        """Called when the dashboard becomes active."""
        self._services.deliveries.start_listening()
        
    def on_authenticate(self):
        access_token = self._state_machine.logged_in_state.property("access_token")
        self._services.deliveries.set_access_token(access_token)

    def _connect_signals(self):
        # TODO: Create states for this.
        self._services.deliveries.delivery_acknowledged.connect(self._on_delivery_acknowledged)
        self._services.deliveries.delivery_update.connect(self._on_delivery_status_update)

    def _on_log_received(self, created_at, level, message):
        self._view.log_table.add_row(created_at, level, message)

    def _on_delivery_acknowledged(self, order_id: str, order: Order):
        # Assuming you extract a displayable address from the data
        try:
            model: DeliveryTableModel = self._view.deliveries_table.model()
            model.add_delivery_acknowledge(order_id, order)
        except ValidationError as e:
            self.logger.exception("Ocorreu um erro ao converter dados da entrega para serem adicionados a tabela! " \
            "Entrega pode ter ser adicionada no Velide mas não será listada.")
        except Exception as e:
            self.logger.exception("Ocorreu um erro inesperado ao adicionar entrega à tabela! " \
            "Entrega pode ter ser adicionada no Velide mas não será listada.")

    def _on_delivery_status_update(self, order_id: str, status: str):
        order = self._services.deliveries.get_order(order_id)
        if order is None:
            # TODO: Raise error on `get_order` instead.
            self.logger.error("Entrega não encontrada! Não foi possível atualizar o seu status.")
            return
        
        self._view.deliveries_table.update_delivery(order_id, order, status)

    def _transform(self, order_id: str, status: DeliveryRowStatus, order: Order) -> DeliveryRowModel:
        return DeliveryRowModel(
            id=order_id,
            status=status,
            order=order
        )