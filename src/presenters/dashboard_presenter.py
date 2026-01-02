from __future__ import annotations
from states.main_state_machine import MainStateMachine
import logging
from PyQt5.QtCore import QObject
from pydantic import ValidationError
from models.delivery_table_model import DeliveryRowModel, DeliveryRowStatus, DeliveryTableModel
from models.velide_delivery_models import Order
from visual.main_view import MainView
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from models.app_context_model import Services

class DashboardPresenter(QObject):
    def __init__(self, view: MainView, services: 'Services', state_machine: MainStateMachine):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._dashboard_view = view.dashboard_screen
        self._services = services
        self._machine = state_machine

        self._machine.logged_in_state.entered.connect(self.on_authenticate)
        self._machine.logged_in_state.dashboard_state.entered.connect(self.start)

        self._connect_signals()

    def start(self):
        """Called when the dashboard becomes active."""
        self._services.deliveries.start_listening()
        
    def on_authenticate(self):
        access_token = self._machine.logged_in_state.property("access_token")
        self._services.websockets.set_access_token(access_token)
        self._services.reconciliation.set_access_token(access_token)
        self._services.deliveries.set_access_token(access_token)

    def _connect_signals(self):
        # TODO: Create states for this.
        self._services.deliveries.delivery_acknowledged.connect(self._on_delivery_acknowledged)
        self._services.deliveries.delivery_update.connect(self._on_delivery_status_update)
        self._services.websockets.action_received.connect(self._services.velide_action_handler.handle_action)
        self._services.velide_action_handler.delivery_deleted.connect(self._services.deliveries.on_delivery_deleted_in_velide)
        self._services.velide_action_handler.delivery_in_route.connect(self._services.deliveries.on_delivery_route_started_in_velide)
        self._services.velide_action_handler.delivery_delivered.connect(self._services.deliveries.on_delivery_route_ended_in_velide)
        self._services.reconciliation.delivery_in_route.connect(self._services.deliveries.on_reconciliation_detects_route_start)
        self._services.reconciliation.delivery_missing.connect(self._services.deliveries.on_reconciliation_misses_delivery)

    def _on_log_received(self, created_at, level, message):
        self._dashboard_view.log_table.add_row(created_at, level, message)

    def _on_delivery_acknowledged(self, order_id: str, order: Order):
        # Assuming you extract a displayable address from the data
        try:
            model: DeliveryTableModel = self._dashboard_view.deliveries_table.model()
            model.add_delivery_acknowledge(order_id, order)
        except ValidationError:
            self.logger.exception("Ocorreu um erro ao converter dados da entrega para serem adicionados a tabela! " \
            "Entrega pode ter ser adicionada no Velide mas não será listada.")
        except Exception:
            self.logger.exception("Ocorreu um erro inesperado ao adicionar entrega à tabela! " \
            "Entrega pode ter ser adicionada no Velide mas não será listada.")

    def _on_delivery_status_update(self, order_id: str, status: str):
        order = self._services.deliveries.get_order(order_id)
        # Very rare or impossible to happen. Handle it anyways.
        if order is None:
            self.logger.error("Entrega não encontrada! Não foi possível atualizar o seu status.")
            return
        
        self._dashboard_view.deliveries_table.update_delivery(order_id, order, status)

    def _transform(self, order_id: str, status: DeliveryRowStatus, order: Order) -> DeliveryRowModel:
        return DeliveryRowModel(
            id=order_id,
            status=status,
            order=order
        )