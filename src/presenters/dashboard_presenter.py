from __future__ import annotations
from services.strategies.connectable_strategy import ERPFeature, IConnectableStrategy
from states.main_state_machine import MainStateMachine
import logging
from PyQt5.QtCore import QObject
from pydantic import ValidationError
from models.delivery_table_model import (
    DeliveryRowModel,
    DeliveryRowStatus,
    DeliveryTableModel,
)
from models.velide_delivery_models import Order
from utils.connection_state import ConnectionState
from visual.main_view import MainView
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.app_context_model import Services


class DashboardPresenter(QObject):
    def __init__(
        self, view: MainView, 
        services: "Services", 
        state_machine: MainStateMachine,
        strategy: IConnectableStrategy
    ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._dashboard_view = view.dashboard_screen
        self._services = services
        self._machine = state_machine
        self._strategy = strategy

        # Flag to define if `start()` was already called or not
        self._is_started = False

        self._machine.logged_in_state.entered.connect(self.on_authenticate)
        self._machine.logged_in_state.dashboard_state.entered.connect(self.start)

        self._connect_signals()

    def start(self) -> None:
        """Called when the dashboard becomes active."""
        if self._is_started:
            return

        self._is_started = True
        self._services.deliveries.start_listening()

        if ERPFeature.DASHBOARD_FOOTER in self._strategy.capabilities:
            self._dashboard_view.footer_enabled = True

            self._dashboard_view.deliverymen_settings_clicked.connect(
                self._services.deliverymen_retriever.request_deliverymen_mapping_screen
            )

    def on_authenticate(self):
        access_token = self._machine.logged_in_state.property("access_token")
        self._services.reconciliation.set_access_token(access_token)
        self._services.deliveries.set_access_token(access_token)

    def _connect_signals(self):
        ws_fsm = self._machine.websockets_state
        
        # --- State Transitions (Presenter Logic) ---
        # Map FSM States -> View Updates
        # We use lambdas to pass the specific Enum required by the View
        
        # --- UI Updates (Conditional) ---
        # Only connect footer signals if the strategy supports the footer.
        # This prevents "AttributeError: 'NoneType' object has no attribute..."
        if ERPFeature.DASHBOARD_FOOTER in self._strategy.capabilities:
            ws_fsm.s_connecting.entered.connect(
                lambda: self._update_footer(ConnectionState.CONNECTING)
            )
            
            ws_fsm.s_connected.entered.connect(
                lambda: self._update_footer(ConnectionState.CONNECTED)
            )
            
            ws_fsm.s_disconnected.entered.connect(
                lambda: self._update_footer(ConnectionState.DISCONNECTED)
            )
            
            ws_fsm.s_error.entered.connect(
                lambda: self._update_footer(ConnectionState.ERROR)
            )
        
        # --- Data Handling ---
        # Use the FSM's sanitized signal (Gatekeeper)
        ws_fsm.action_ready.connect(
             self._services.velide_action_handler.handle_action
        )

        # TODO: Create states for this.
        self._services.deliveries.delivery_acknowledged.connect(
            self._on_delivery_acknowledged
        )
        self._services.deliveries.delivery_update.connect(
            self._on_delivery_status_update
        )
        self._services.websockets.action_received.connect(
            self._services.velide_action_handler.handle_action
        )
        self._services.velide_action_handler.delivery_deleted.connect(
            self._services.deliveries.on_delivery_deleted_in_velide
        )
        self._services.velide_action_handler.delivery_in_route.connect(
            self._services.deliveries.on_delivery_route_started_in_velide
        )
        self._services.velide_action_handler.delivery_delivered.connect(
            self._services.deliveries.on_delivery_route_ended_in_velide
        )
        self._services.reconciliation.delivery_in_route.connect(
            self._services.deliveries.on_reconciliation_detects_route_start
        )
        self._services.reconciliation.delivery_missing.connect(
            self._services.deliveries.on_reconciliation_misses_delivery
        )

    def _on_log_received(self, created_at, level, message):
        self._dashboard_view.log_table.add_row(created_at, level, message)

    def _on_delivery_acknowledged(self, order_id: str, order: Order):
        # Assuming you extract a displayable address from the data
        try:
            model: DeliveryTableModel = self._dashboard_view.deliveries_table.model()
            model.add_delivery_acknowledge(order_id, order)
        except ValidationError:
            self.logger.exception(
                "Ocorreu um erro ao converter dados da entrega " \
                "para serem adicionados a tabela! "
                "Entrega pode ter ser adicionada no Velide mas não será listada."
            )
        except Exception:
            self.logger.exception(
                "Ocorreu um erro inesperado ao adicionar entrega à tabela! "
                "Entrega pode ter ser adicionada no Velide mas não será listada."
            )

    def _on_delivery_status_update(self, order_id: str, status: str):
        order = self._services.deliveries.get_order(order_id)
        # Very rare or impossible to happen. Handle it anyways.
        if order is None:
            self.logger.error(
                "Entrega não encontrada! Não foi possível atualizar o seu status."
            )
            return

        self._dashboard_view.deliveries_table.update_delivery(order_id, order, status)

    def _update_footer(self, state: ConnectionState):
        """Helper to safely update the footer if it exists."""
        # Even with the capability check, the view might not have initialized 
        # the footer widget yet (e.g., if called before 'start()').
        if self._dashboard_view.footer:
            self._dashboard_view.footer.update_connection_state(state)

    def _transform(
        self, order_id: str, status: DeliveryRowStatus, order: Order
    ) -> DeliveryRowModel:
        return DeliveryRowModel(id=order_id, status=status, order=order)
