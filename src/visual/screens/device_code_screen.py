from PyQt5.QtWidgets import QPushButton, QLabel, QWidget, QVBoxLayout, QSpacerItem, QStackedWidget
from PyQt5.QtCore import Qt, pyqtSignal
from utils.device_code import DeviceCodeDict
from visual.fonts import get_fonts
from config import config
from visual.screens.loading_screen import LoadingScreen
from visual.screens.device_code_display import DeviceCodeDisplay

class DeviceCodeScreen(QWidget):
    expired = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.fonts = get_fonts()

        self.main_layout = QVBoxLayout()

        self.explainer_label = QLabel(f"Configure o seu servidor do <b>{config.target_system.value}</b><br/>para conectar-se com o <b>Velide</b>.")
        self.explainer_label.setFont(self.fonts["regular"])
        self.explainer_label.setAlignment(Qt.AlignCenter)

        self.stack = QStackedWidget()

        self.main_layout.addSpacing(48)
        self.main_layout.addWidget(self.explainer_label, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(24)
        self.main_layout.addWidget(self.stack, alignment=Qt.AlignHCenter)

        self.device_code_loading = LoadingScreen("Solicitando código de autenticação,<br/>por favor aguarde...")

        # self.main_layout.setSpacing(4)
        self.stack.addWidget(self.device_code_loading)
        self.stack.setCurrentIndex(0)

        self.setLayout(self.main_layout)

    def set_device_code(self, device_code: DeviceCodeDict):
        self.device_code = device_code

    def display_qr_code(self):
        if not hasattr(self, "device_code_display"):
            self.device_code_display = DeviceCodeDisplay(self.device_code)
            self.device_code_display.expired.connect(self.expired.emit)
            self.stack.addWidget(self.device_code_display)
        self.stack.setCurrentIndex(1)