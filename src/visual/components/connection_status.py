from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel

from utils.connection_state import ConnectionColors, ConnectionState
from visual.fonts import get_fonts

class ConnectionStatus(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fonts = get_fonts()
        self._setup_ui()
        self.set_status(ConnectionState.DISCONNECTED) # Default state

    def _setup_ui(self):
        # Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 1. The Indicator Ball
        self.indicator = QLabel()
        self.indicator.setFixedSize(12, 12)
        # We set the base style for the circle here
        self.indicator.setStyleSheet("border-radius: 6px;") 

        # 2. The Text Label
        self.label = QLabel()
        # Tailwind Gray-700
        self.label.setStyleSheet("font-weight: 500;") 
        self.label.setFont(self.fonts["regular_small"])

        layout.addWidget(self.indicator)
        layout.addWidget(self.label)

    def set_status(self, state: ConnectionState):
        """
        Public API to change the visual state.
        """
        color = ConnectionColors.get_color(state)
        text = ConnectionColors.get_label(state)

        # Update Indicator Color
        self.indicator.setStyleSheet(f"background-color: {color}; border-radius: 6px;")
        
        # Update Text
        self.label.setText(text)