import os
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QPushButton,
    QLabel,
    QStackedWidget,
    QHBoxLayout,
)
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QStateMachine, QState
from screeninfo import get_monitors

from services.auth_service import AuthService
from utils.bundle_dir import BUNDLE_DIR
from visual.screens.cds_screen import CdsScreen
from visual.screens.device_code_screen import DeviceCodeScreen
from visual.screens.initial_screen import InitialScreen
from visual.screens.device_code_error_screen import DeviceCodeErrorScreen

WINDOW_SIZE = [600, 600]

class MainView(QWidget):
    def __init__(self):
        super().__init__()

        self._initialize_window()
        self._create_screens()

    def _initialize_window(self):
        self.setWindowTitle("Velide Middleware")

         # Set the window icon
        icon_path = os.path.join(BUNDLE_DIR, "resources", "velide.png")  # Use an appropriate icon path
        self.setWindowIcon(QIcon(icon_path))

        self.setGeometry(round((get_monitors()[0].width / 2) - (WINDOW_SIZE[0] / 2)), round((get_monitors()[0].height / 2) - (WINDOW_SIZE[1] / 2)), WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.setFixedSize(WINDOW_SIZE[0], WINDOW_SIZE[1])

    def _create_screens(self):
        self.stack = QStackedWidget()

        self.initial_screen = InitialScreen()
        self.device_code_screen = DeviceCodeScreen()
        self.device_code_error_screen = DeviceCodeErrorScreen()
        self.cds_screen = CdsScreen()

        self.stack.addWidget(self.initial_screen)
        self.stack.addWidget(self.device_code_screen) 
        self.stack.addWidget(self.device_code_error_screen) 
        self.stack.addWidget(self.cds_screen) 

        self._layout = QVBoxLayout(self) # 'self' sets the layout on MainView
        self._layout.setContentsMargins(0, 0, 0, 0) # Optional: remove padding
        self._layout.addWidget(self.stack)

    def show_screen_by_index(self, index: int):
        """A simple method the Presenter can call to switch screens."""
        self.stack.setCurrentIndex(index)