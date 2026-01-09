from typing import Dict, List, Optional
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QSizePolicy,
)
from PyQt5.QtCore import pyqtSignal, Qt

from models.base_models import BaseLocalDeliveryman
from models.velide_delivery_models import DeliverymanResponse
from visual.components.mapping_table_view import MappingTableView
from visual.fonts import get_fonts
from visual.screens.loading_screen import LoadingScreen


class DeliverymenMappingScreen(QWidget):
    save_config = pyqtSignal()
    cancel_config = pyqtSignal()  # New signal for the cancel action

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fonts = get_fonts()

        self.stack_layout = QVBoxLayout()
        self.stack = QStackedWidget()
        self.main_widget = QWidget()

        self.description_label = QLabel(
            text="Relacione os entregadores do Velide com os cadastrados no sistema."
        )
        self.description_label.setFont(self.fonts["regular_small"])
        self.description_label.setWordWrap(True)

        self.main_layout = QVBoxLayout()
        self.deliverymen_config_table = MappingTableView()

        self.instructions_label = QLabel(
            "Para cada <b>Entregador Velide</b> à esquerda, "
            "selecione o <b>Entregador Local</b> correspondente à direita."
        )
        self.instructions_label.setFont(self.fonts["regular_small"])
        self.instructions_label.setWordWrap(True)

        # --- Buttons Section ---
        self.save_button = QPushButton("Salvar")
        self.save_button.setCursor(Qt.PointingHandCursor)
        self.save_button.setFont(self.fonts["bold"])
        self.save_button.clicked.connect(self.save_config)
        # Make save button expand to fill available space if desired, or keep fixed
        self.save_button.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )

        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setObjectName("neutral")
        self.cancel_button.setFont(self.fonts["regular"])
        self.cancel_button.clicked.connect(self.cancel_config)
        self.cancel_button.hide()  # Hidden by default (Auto-mode behavior)
        self.cancel_button.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )

        # Horizontal layout to put Cancel and Save side-by-side
        self.buttons_layout = QHBoxLayout()
        self.buttons_layout.setSpacing(12)
        self.buttons_layout.addWidget(self.cancel_button)
        self.buttons_layout.addWidget(self.save_button)

        self.footer = QWidget()
        self.footer_layout = QVBoxLayout()
        self.footer_layout.setSpacing(0)
        self.footer_layout.setContentsMargins(0, 0, 0, 0)
        
        # Add the horizontal buttons layout to the footer
        self.footer_layout.addLayout(self.buttons_layout)
        self.footer.setLayout(self.footer_layout)

        self.main_layout.addWidget(self.description_label)
        self.main_layout.addWidget(self.deliverymen_config_table)
        self.main_layout.addWidget(self.instructions_label)
        self.main_layout.addWidget(self.footer)
        self.main_layout.setSpacing(12)

        self.loading_screen = LoadingScreen(
            "Buscando entregadores,<br/>por favor aguarde..."
        )

        self.main_widget.setLayout(self.main_layout)
        self.stack_layout.addWidget(self.stack)
        self.setLayout(self.stack_layout)

        self.stack.addWidget(self.loading_screen)
        self.stack.addWidget(self.main_widget)
        self.stack.setCurrentIndex(0)
        self.set_cancel_visible(True)

    def populate_table(
        self,
        source_items: List[DeliverymanResponse],
        destination_options: List[BaseLocalDeliveryman],
        default_mappings: Optional[Dict[str, str]] = None,
        headers: Optional[List[str]] = None,
    ):
        self.deliverymen_config_table.populate_table(
            source_items, destination_options, default_mappings, headers
        )
        self.stack.setCurrentIndex(1)

    def set_screen(self, index: int):
        self.stack.setCurrentIndex(index)

    def get_mappings(self):
        return self.deliverymen_config_table.get_mappings()

    def set_cancel_visible(self, visible: bool):
        """
        Controls the visibility of the Cancel button.
        Called by the Presenter/State logic based on manual vs auto mode.
        """
        self.cancel_button.setVisible(visible)