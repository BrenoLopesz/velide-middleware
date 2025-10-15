from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QSizePolicy
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QCursor

from visual.fonts import get_fonts

class ErrorScreen(QWidget):
    retry = pyqtSignal()
    skip = pyqtSignal()

    def __init__(self):
        super().__init__()
        fonts = get_fonts()
        
        self.main_layout = QVBoxLayout()
        
        self.main_title = QLabel("Não foi possível realizar<br/>a atualização.")
        self.main_title.setFont(fonts["bold"])
        self.main_title.setAlignment(Qt.AlignCenter)

        self.error_description = QLabel("Ocorreu um erro inesperado.")
        self.error_description.setFont(fonts["regular"])
        self.error_description.setObjectName("error")
        self.error_description.setAlignment(Qt.AlignCenter)
        self.error_description.setWordWrap(True)
        self.error_description.setMaximumWidth(600-256)

        # Set the horizontal policy to Preferred and the VERTICAL policy to Fixed.
        # This tells the layout: "You can manage my width, but my height is
        # fixed to whatever my content requires. DO NOT shrink me vertically."
        self.error_description.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        self.button = QPushButton('Tentar Novamente')
        self.button.setFont(fonts["bold"])
        self.button.setCursor(QCursor(Qt.PointingHandCursor))
        self.button.clicked.connect(self.retry)

        self.skip_label = QLabel("<span style=\"text-decoration: underline\">Pular atualização</span>")
        self.skip_label.setCursor(QCursor(Qt.PointingHandCursor))
        self.skip_label.setTextFormat(Qt.RichText)
        self.skip_label.setFont(fonts["light"])
        self.skip_label.setAlignment(Qt.AlignCenter)
        self.button.clicked.connect(self.skip)

        self.main_layout.addStretch()
        self.main_layout.addWidget(self.main_title, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(96)
        self.main_layout.addWidget(self.error_description, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(96)
        self.main_layout.addWidget(self.button, alignment=Qt.AlignHCenter)
        self.main_layout.addWidget(self.skip_label, alignment=Qt.AlignHCenter)
        self.main_layout.addStretch()

        self.setLayout(self.main_layout)

    def set_error_description(self, error_description: str):
        self.error_description.setText(error_description)
        self.error_description.adjustSize()
        self.adjustSize() 