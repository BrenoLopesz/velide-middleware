import math
from PyQt5.QtCore import Qt, pyqtProperty
from PyQt5.QtWidgets import QLabel
from PyQt5.QtGui import QPainter, QTransform, QPixmap

class LoadingIcon(QLabel):
    def __init__(self, size=96):
        super().__init__()
        self._rotation = 0
        self.pixmap = QPixmap('resources/loading.png')

        # --- FIX 1: Correctly scale the pixmap and reassign it ---
        # We scale it to the desired base size, keeping the aspect ratio.
        self.pixmap = self.pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # --- FIX 2: Calculate the diagonal and set the widget size ---
        # The widget's bounding box needs to be the size of the pixmap's diagonal
        # to prevent clipping during rotation.
        diagonal = math.ceil(math.sqrt(self.pixmap.width()**2 + self.pixmap.height()**2))
        self.setFixedSize(diagonal, diagonal)

        # We no longer need self.setPixmap() as the paintEvent handles all drawing.
        # self.resize() is also replaced by setFixedSize().

    def setRotation(self, angle):
        """Sets the rotation angle."""
        self._rotation = angle
        self.update()  # Trigger a repaint

    def getRotation(self):
        """Gets the rotation angle."""
        return self._rotation

    # Define a Q_PROPERTY for rotation, so QPropertyAnimation can use it
    rotation = pyqtProperty(float, getRotation, setRotation)

    def paintEvent(self, event):
         # Create a QPainter object to handle drawing
        painter = QPainter(self)
        
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        # Calculate the center point of the QLabel
        center = self.rect().center()

        # Apply rotation transform
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(self._rotation)
        transform.translate(-center.x(), -center.y())

        # Set the transform to the painter
        painter.setTransform(transform)

        # Draw the pixmap centered in the label
        pixmap_center = self.pixmap.rect().center()
        x = center.x() - pixmap_center.x()
        y = center.y() - pixmap_center.y()
        painter.drawPixmap(x, y, self.pixmap)

        painter.end()