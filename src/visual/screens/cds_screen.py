from PyQt5.QtWidgets import QWidget, QVBoxLayout

from visual.fonts import get_fonts
from visual.components.deliveries_table import DeliveriesTable
from visual.components.log_table import LogTable

class CdsScreen(QWidget):
    def __init__(self):
        super().__init__()
        fonts = get_fonts()
        
        self.main_layout = QVBoxLayout(self)
        self.log_table = LogTable()

        self.deliveries_table = DeliveriesTable()

        self.main_layout.addWidget(self.deliveries_table)
        self.main_layout.addSpacing(12)
        self.main_layout.addWidget(self.log_table)

        self.setLayout(self.main_layout)