from typing import Optional
from PyQt5.QtCore import QObject, QStateMachine, QState, QFinalState
from services.update_checker_service import UpdateCheckerService
from models.config import InstallerConfig
from services.config_load_service import ConfigLoadService
from main_view import MainView

class AppPresenter(QObject):
    def __init__(self, view: MainView, config_load_service: ConfigLoadService):
        super().__init__()
        self._view = view
        self._config_load_service = config_load_service
        self._update_checker_service = UpdateCheckerService()
        self._machine = QStateMachine(self)

        self._create_states()
        self._connect_actions()
        self._build_state_machine()

    def run(self):
        self._machine.start()
        self._view.show()
        self._config_load_service.load_config()

    def _on_config_found(self, config: InstallerConfig):
        self._update_checker_service.check_for_update(config)

    def _on_new_version_found(self, zip_url: str, new_version: str):
        self._view.update_screen.on_new_version_found(new_version)
        # TODO: Install update

    def _create_states(self):
        self.initial_state = QState()
        self.updating_state = QState()
        self.finished_state = QFinalState()
        self.error_state = QFinalState()
    
    def _connect_actions(self):
        self._config_load_service.error.connect(self.display_error_screen)
        self._config_load_service.config_found.connect(self._on_config_found)

        self._update_checker_service.error.connect(self.display_error_screen)
        self._update_checker_service.update_found.connect(self._on_new_version_found)

        # --- UI Screen Switching ---
        self.updating_state.entered.connect(lambda: self._view.show_screen_by_index(0))
        self.finished_state.entered.connect(lambda: self._view.show_screen_by_index(1))
        self.error_state.entered.connect(lambda: self._view.show_screen_by_index(2))

    def _build_state_machine(self):
        """Adds states and transitions to the state machine."""
        states = [
            self.initial_state, self.updating_state, self.finished_state, self.error_state
        ]
        for state in states:
            self._machine.addState(state)

        # Initial -> Updating
        self.initial_state.addTransition(self._config_load_service.config_found, self.updating_state)
        # Initial -> Error
        self.initial_state.addTransition(self._config_load_service.error, self.error_state)

        # Updating -> Finished
        self.updating_state.addTransition(self._update_checker_service.update_found, self.finished_state)
        # Updating -> Error
        self.updating_state.addTransition(self._update_checker_service.error, self.error_state)
            
        self._machine.setInitialState(self.initial_state)

    def display_error_screen(self, error_message: str, stacktrace: Optional[str]):
        self._view.error_screen.set_error_description(error_message)
