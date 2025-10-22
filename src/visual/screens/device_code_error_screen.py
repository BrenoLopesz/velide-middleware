from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QSizePolicy
from PyQt5.QtCore import Qt, pyqtSignal

from visual.fonts import get_fonts

class ErrorScreen(QWidget):
    retry = pyqtSignal()

    def __init__(self):
        super().__init__()
        fonts = get_fonts()
        
        self.main_layout = QVBoxLayout()
        
        self.main_title = QLabel("Ocorreu um erro inseperado.")
        # self.main_title = QLabel("Não foi possível realizar<br/>a autenticação.")
        self.main_title.setFont(fonts["bold"])
        self.main_title.setAlignment(Qt.AlignCenter)

        self.error_description = QLabel("Tente novamente.")
        # self.error_description = QLabel("QR Code expirado.")
        self.error_description.setFont(fonts["regular"])
        self.error_description.setObjectName("error")
        self.error_description.setAlignment(Qt.AlignCenter)
        self.error_description.setWordWrap(True)

        self.error_description.setSizePolicy(
            QSizePolicy.Expanding, 
            QSizePolicy.Expanding
        )

        # Set the horizontal policy to Preferred and the VERTICAL policy to Fixed.
        # This tells the layout: "You can manage my width, but my height is
        # fixed to whatever my content requires. DO NOT shrink me vertically."
        self.error_description.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self.button = QPushButton('Tentar Novamente')
        self.button.setFont(fonts["bold"])
        self.button.clicked.connect(self.retry)

        self.main_layout.addStretch()
        self.main_layout.addWidget(self.main_title, alignment=Qt.AlignHCenter)
        self.main_layout.addWidget(self.error_description, alignment=Qt.AlignHCenter)
        self.main_layout.addWidget(self.button, alignment=Qt.AlignHCenter)
        self.main_layout.addStretch()

        self.main_layout.setSpacing(64)

        self.setLayout(self.main_layout)

    def set_error_title(self, error_title: str):
        self.main_title.setText(error_title)
        self.main_title.adjustSize()
        self.adjustSize()

    def set_error_description(self, error_description: str):
        self.error_description.setText(error_description)
        self.error_description.adjustSize()
        self.adjustSize() 