# src/presenters/app_presenter.py

from PyQt5.QtCore import QObject, QStateMachine, QState
from services.deliveries_service import DeliveriesService
from presenters.dashboard_presenter import DashboardPresenter
from presenters.device_code_presenter import DeviceCodePresenter
from visual.main_view import MainView
from services.auth_service import AuthService
from services.restart_service import restart_application

class AppPresenter(QObject):
    def __init__(self, view: MainView, auth_service: AuthService, delivery_service: DeliveriesService):
        super().__init__()
        self._view = view
        self._auth_service = auth_service
        self._delivery_service = delivery_service
        self._machine = QStateMachine(self)

        self._last_error_message = ""

        self._device_code_presenter = DeviceCodePresenter(self._auth_service, self._view)
        self._dashboard_presenter = DashboardPresenter(self._view.cds_screen, self._delivery_service)

        self._create_states()
        self._connect_actions()
        self._build_state_machine()
    
    def run(self):
        self._machine.start()
        self._view.show()

    def _create_states(self):
        self.initial_state = QState()
        self.device_flow_state = QState()
        self.dashboard_state = QState()
        self.error_state = QState()
        self.restart_state = QState() # Renamed for clarity
    
    def _connect_actions(self):
        self._device_code_presenter.authenticated.connect(self._dashboard_presenter.on_authenticate)

        # --- UI Screen Switching ---
        self.initial_state.entered.connect(lambda: self._view.show_screen_by_index(0))
        self.device_flow_state.entered.connect(lambda: self._view.show_screen_by_index(1))
        self.dashboard_state.entered.connect(lambda: self._view.show_screen_by_index(3))
        self.dashboard_state.entered.connect(self._dashboard_presenter.start)

        # --- Logic Triggers ---
        self.device_flow_state.entered.connect(self._device_code_presenter.on_start)
        self.restart_state.entered.connect(restart_application)

        # Add a direct connection to CATCH the error message data
        self._device_code_presenter.error.connect(self._cache_error_message)
        # The state machine uses this to show the screen
        self.error_state.entered.connect(self._show_error_screen)

    def _build_state_machine(self):
        """Adds states and transitions to the state machine."""
        states = [
            self.initial_state, self.device_flow_state, self.dashboard_state, 
            self.error_state, self.restart_state
        ]
        for state in states:
            self._machine.addState(state)
            
        self._machine.setInitialState(self.initial_state)

        # --- Transitions ---
        self.initial_state.addTransition(self._view.initial_screen.start_device_flow, self.device_flow_state)

        # Move to dashboard whenever authenticated
        self.initial_state.addTransition(self._device_code_presenter.authenticated, self.dashboard_state)
        self.device_flow_state.addTransition(self._device_code_presenter.authenticated, self.dashboard_state)
        
        # The transition to the error state is triggered by the presenter's signal
        self.device_flow_state.addTransition(self._device_code_presenter.error, self.error_state)

        self.error_state.addTransition(self._view.device_code_error_screen.retry, self.restart_state)

    def _cache_error_message(self, message: str):
        self._last_error_message = message

    def _show_error_screen(self):
        """
        This slot is connected to error_state.entered and has NO arguments.
        It uses the cached message to update the view.
        """
        self._view.device_code_error_screen.set_error_description(self._last_error_message)
        self._view.show_screen_by_index(2)