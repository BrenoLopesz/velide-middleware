import os
from typing import Optional
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QStackedWidget,
)
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QIcon
from screeninfo import get_monitors

from utils.bundle_dir import BUNDLE_DIR
from utils.device_code import DeviceCodeDict
from utils.tray_manager import AppTrayIcon
from visual.screens.dashboard_screen import DashboardScreen
from visual.screens.deliverymen_mapping_screen import DeliverymenMappingScreen
from visual.screens.device_code_screen import DeviceCodeScreen
from visual.screens.initial_screen import InitialScreen
from visual.screens.device_code_error_screen import ErrorScreen

WINDOW_SIZE = [600, 600]


class MainView(QWidget):
    device_code_expired = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.tray_icon: Optional[AppTrayIcon] = None

        self._initialize_window()
        self._create_screens()

    def _initialize_window(self):
        self.setWindowTitle("Velide Middleware")

        # Set the window icon
        icon_path = os.path.join(
            BUNDLE_DIR, "resources", "velide.png"
        )  # Use an appropriate icon path
        self.setWindowIcon(QIcon(icon_path))

        self.setGeometry(
            round((get_monitors()[0].width / 2) - (WINDOW_SIZE[0] / 2)),
            round((get_monitors()[0].height / 2) - (WINDOW_SIZE[1] / 2)),
            WINDOW_SIZE[0],
            WINDOW_SIZE[1],
        )
        self.setFixedSize(WINDOW_SIZE[0], WINDOW_SIZE[1])

    def set_tray_icon(self, tray_icon: AppTrayIcon):
        """Receives the tray icon instance so we can use it for notifications."""
        self.tray_icon = tray_icon

    def _create_screens(self):
        self.stack = QStackedWidget()

        self.initial_screen = InitialScreen()
        self.device_code_screen = DeviceCodeScreen()
        self.device_code_error_screen = ErrorScreen()
        self.dashboard_screen = DashboardScreen()
        self.deliverymen_mapping_screen = DeliverymenMappingScreen()

        self.device_code_screen.expired.connect(self.device_code_expired.emit)

        self.stack.addWidget(self.initial_screen)
        self.stack.addWidget(self.device_code_screen)
        self.stack.addWidget(self.device_code_error_screen)
        self.stack.addWidget(self.dashboard_screen)
        self.stack.addWidget(self.deliverymen_mapping_screen)

        self._layout = QVBoxLayout(self)  # 'self' sets the layout on MainView
        self._layout.setContentsMargins(0, 0, 0, 0)  # Optional: remove padding
        self._layout.addWidget(self.stack)

    def set_device_code_and_qr(self, device_code: DeviceCodeDict):
        self.device_code_screen.set_device_code(device_code)
        self.device_code_screen.display_qr_code()

    def show_screen_by_index(self, index: int):
        """A simple method the Presenter can call to switch screens."""
        self.stack.setCurrentIndex(index)
