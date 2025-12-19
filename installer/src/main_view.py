import os
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QStackedWidget,
)
from PyQt5.QtGui import QIcon
from screeninfo import get_monitors

from utils.bundle_dir import BUNDLE_DIR
from visual.screens.finished_screen import FinishedScreen
from visual.screens.update_screen import UpdateScreen
from visual.screens.error_screen import ErrorScreen

WINDOW_SIZE = [600, 600]

class MainView(QWidget):
    def __init__(self):
        super().__init__()

        self._initialize_window()
        self._create_screens()

    def _initialize_window(self):
        self.setWindowTitle("Instalador Velide Middleware")

         # Set the window icon
        icon_path = os.path.join(BUNDLE_DIR, "resources", "velide.png")  # Use an appropriate icon path
        self.setWindowIcon(QIcon(icon_path))

        self.setGeometry(round((get_monitors()[0].width / 2) - (WINDOW_SIZE[0] / 2)), round((get_monitors()[0].height / 2) - (WINDOW_SIZE[1] / 2)), WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.setFixedSize(WINDOW_SIZE[0], WINDOW_SIZE[1])

    def _create_screens(self):
        self.stack = QStackedWidget()

        self.update_screen = UpdateScreen()
        self.finished_screen = FinishedScreen()
        self.error_screen = ErrorScreen()
        
        self.stack.addWidget(self.update_screen)
        self.stack.addWidget(self.finished_screen) 
        self.stack.addWidget(self.error_screen) 

        self._layout = QVBoxLayout(self) # 'self' sets the layout on MainView
        self._layout.setContentsMargins(0, 0, 0, 0) # Optional: remove padding
        self._layout.addWidget(self.stack)

    def show_screen_by_index(self, index: int):
        """A simple method the Presenter can call to switch screens."""
        self.stack.setCurrentIndex(index)