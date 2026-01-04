# src/presenters/device_code_presenter.py

from PyQt5.QtCore import QObject, pyqtSignal
from states.main_state_machine import MainStateMachine
from visual.main_view import MainView


class DeviceCodePresenter(QObject):
    error = pyqtSignal(str)

    def __init__(self, view: MainView, state_machine: MainStateMachine):
        super().__init__()
        self._view = view
        self._state_machine = state_machine

        self._device_flow_state = self._state_machine.logged_out_state.device_flow_state
        self._device_flow_state.waiting_for_login.entered.connect(
            self._on_device_code_received
        )

        self._view.device_code_expired.connect(self.on_expire)

    def _on_device_code_received(self):
        """Handles receiving device code and updates the view."""
        code_data = self._device_flow_state.waiting_for_login.property("code_data")
        self._view.set_device_code_and_qr(code_data)

    def on_expire(self):
        # TODO: Remove this
        self.error.emit("Código do dispositivo expirado.")
