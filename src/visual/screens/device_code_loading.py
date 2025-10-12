from PyQt5.QtWidgets import QLabel, QWidget, QVBoxLayout
from PyQt5.QtCore import Qt, QPropertyAnimation, pyqtProperty
from visual.components.loading_icon import LoadingIcon
from visual.fonts import get_fonts

class DeviceCodeLoading(QWidget):
    def __init__(self):
        super().__init__()
        self.fonts = get_fonts()

        self.main_layout = QVBoxLayout()
        
        self.add_loading()

        self.setLayout(self.main_layout)

    def add_loading(self):
        self.add_loading_icon() 
        self.loading_label = QLabel("Solicitando código de autenticação,<br/>por favor aguarde...")
        self.loading_label.setFont(self.fonts["regular"])
        self.loading_label.setAlignment(Qt.AlignCenter)
        self.main_layout.addStretch()
        self.main_layout.addWidget(self.loading_icon, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(64)
        self.main_layout.addWidget(self.loading_label, alignment=Qt.AlignHCenter)
        self.main_layout.addStretch()


    @pyqtProperty(int)
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, rotation):
        self._rotation = rotation
        self.valueChanged.emit(rotation)

    def add_loading_icon(self):
        self.loading_icon = LoadingIcon()
        self.loading_icon.move(round(300 - self.loading_icon.width() / 2), round(300 - self.loading_icon.height() / 2))
        self.anim = QPropertyAnimation(self.loading_icon, b"rotation")
        self.anim.setDuration(1600)  
        self.anim.setStartValue(0)   
        self.anim.setEndValue(360)   
        self.anim.setLoopCount(-1)
        self.anim.start()