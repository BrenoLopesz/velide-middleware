from PyQt5.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QCursor

from utils.connection_state import ConnectionState
from visual.components.connection_status import ConnectionStatus
from visual.fonts import get_fonts

class DashboardFooter(QWidget):
    # Optional: Custom signal if you want to forward the click event
    deliverymen_settings_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fonts = get_fonts()
        self._setup_ui()

    def _setup_ui(self):

        # 1. Main Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        # 2. Left Component: Connection Status
        self.connection_status = ConnectionStatus()
        
        # 3. Right Component: Settings Link
        self.deliverymen_settings_btn = QPushButton("Configurar Entregadores")
        self.deliverymen_settings_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.deliverymen_settings_btn.setFont(self.fonts["regular_small"])
        
        # Style the button to look like a link (Tailwind Blue-600 hover interaction)
        self.deliverymen_settings_btn.setStyleSheet("""
            QPushButton {
                border: none;
                background: transparent;
                color: #2563eb; /* Blue-600 */
                text-decoration: none;
                font-weight: bold;
            }
            QPushButton:hover {
                text-decoration: underline;
                color: #1d4ed8; /* Blue-700 */
            }
        """)
        
        # Connect internal click to custom signal (optional, but good practice)
        self.deliverymen_settings_btn.clicked.connect(
            self.deliverymen_settings_clicked
        )

        # 4. Assembly
        layout.addWidget(self.connection_status)
        layout.addStretch() # This pushes the next item to the far right
        layout.addWidget(self.deliverymen_settings_btn)

    # --- Public API for the Presenter ---

    def update_connection_state(self, state: ConnectionState):
        """Passes the state down to the child component."""
        self.connection_status.set_status(state)

    def connect_settings_action(self, callback):
        """Allows the presenter to attach a function to the settings button."""
        self.deliverymen_settings_clicked.connect(callback)