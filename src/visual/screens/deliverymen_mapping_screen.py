from typing import Dict, List, Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QStackedWidget
from PyQt5.QtCore import pyqtSignal

from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from visual.components.mapping_table_view import MappingTableView
from visual.fonts import get_fonts
from visual.screens.loading_screen import LoadingScreen

class DeliverymenMappingScreen(QWidget):
    save_config = pyqtSignal()

    def __init__(self, parent = None):
        super().__init__(parent)
        self.fonts = get_fonts()

        self.stack_layout = QVBoxLayout()
        self.stack = QStackedWidget()
        self.main_widget = QWidget()

        self.description_label = QLabel(text="Relacione os entregadores do Velide com os cadastrados no sistema.")
        self.description_label.setFont(self.fonts["regular_small"])
        self.description_label.setWordWrap(True)

        self.main_layout = QVBoxLayout()
        self.deliverymen_config_table = MappingTableView()

        self.instructions_label = QLabel(
            "Para cada <b>Entregador Velide</b> à esquerda, selecione o <b>Entregador Local</b> correspondente à direita."
        )
        self.instructions_label.setFont(self.fonts["regular_small"])
        self.instructions_label.setWordWrap(True)

        self.save_button = QPushButton('Salvar')
        self.save_button.setFont(self.fonts["bold"])
        self.save_button.clicked.connect(self.save_config)

        self.footer = QWidget()
        self.footer_layout = QVBoxLayout()
        self.footer_layout.setSpacing(12)
        self.footer_layout.addWidget(self.save_button)
        self.footer.setLayout(self.footer_layout)

        self.main_layout.addWidget(self.description_label)
        self.main_layout.addWidget(self.deliverymen_config_table)
        self.main_layout.addWidget(self.instructions_label)
        self.main_layout.addWidget(self.footer)
        self.main_layout.setSpacing(12)
        
        self.loading_screen = LoadingScreen("Buscando entregadores,<br/>por favor aguarde...")

        self.main_widget.setLayout(self.main_layout)
        self.stack_layout.addWidget(self.stack)
        self.setLayout(self.stack_layout)
        
        self.stack.addWidget(self.loading_screen)
        self.stack.addWidget(self.main_widget)
        self.stack.setCurrentIndex(0)

    def populate_table(
            self,
            source_items: List[DeliverymanResponse],
            destination_options: List[BaseLocalDeliveryman],
            default_mappings: Optional[Dict[str, str]] = None,
            headers: Optional[List[str]] = None
        ):
        self.deliverymen_config_table.populate_table(source_items, destination_options, default_mappings, headers)
        self.stack.setCurrentIndex(1)

    def set_screen(self, index: int):
        self.stack.setCurrentIndex(index)

    def get_mappings(self):
        return self.deliverymen_config_table.get_mappings()