from PyQt5.QtWidgets import QPushButton, QLabel, QWidget, QVBoxLayout
from PyQt5.QtCore import Qt, pyqtSignal
from visual.fonts import get_fonts
from config import config


class InitialScreen(QWidget):
    on_request_device_flow_start = pyqtSignal()

    def __init__(self):
        super().__init__()
        fonts = get_fonts()
        main_layout = QVBoxLayout()

        self.button = QPushButton("Conectar com Velide")
        self.button.setFont(fonts["bold"])
        self.button.clicked.connect(self.on_button_press)

        self.explainer_label = QLabel(
            "Configure o seu servidor do "
            f"<b>{config.target_system.value}</b><br/>"
            "para conectar-se com o <b>Velide</b>."
        )
        self.explainer_label.setFont(fonts["regular"])
        self.explainer_label.setAlignment(Qt.AlignCenter)

        self.cta_label = QLabel(
            "<br>Primeiro, conecte esse dispositivo<br/>com sua conta Velide.<b/>"
        )
        self.cta_label.setFont(fonts["bold"])
        self.cta_label.setAlignment(Qt.AlignCenter)

        # Add a flexible space at the top to push everything down.
        main_layout.addStretch(1)
        main_layout.addWidget(self.explainer_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(48)
        main_layout.addWidget(self.cta_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(24)
        main_layout.addWidget(self.button, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(120)

        main_layout.setSpacing(20)

        self.setLayout(main_layout)

    def on_button_press(self):
        self.on_request_device_flow_start.emit()
