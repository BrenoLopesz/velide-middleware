from PyQt5.QtWidgets import QLabel
from PyQt5.QtGui import QPixmap

class VelideIcon(QLabel):
    def __init__(self):
        super().__init__()
        pixmap = QPixmap('resources/velide_36.png')
        pixmap = pixmap.scaledToWidth(36)
        pixmap = pixmap.scaledToHeight(36)
        self.setPixmap(pixmap)