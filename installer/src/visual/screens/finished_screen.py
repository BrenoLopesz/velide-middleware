from typing import Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import Qt

from visual.fonts import get_fonts

class FinishedScreen(QWidget):
    def __init__(self):
        super().__init__()
        self._fonts = get_fonts()

        main_layout = QVBoxLayout()

        self.update_label = QLabel("Atualização Concluída.")
        self.update_label.setObjectName("title")
        self.update_label.setFont(self._fonts["bold"])
        self.update_label.setAlignment(Qt.AlignCenter)

        self.info_label = QLabel(f"Nova versão adquirida:<br/><b>v0.0.0</b><br/>Hoje<br/><br/>Versão anterior:<br/><b>Nenhuma</b>")
        # self.info_label.setObjectName("title")
        self.info_label.setFont(self._fonts["regular"])
        self.info_label.setAlignment(Qt.AlignCenter)

        self.new_version_label = QLabel(f"A integração irá iniciar em breve,<br/>por favor aguarde.")
        self.new_version_label.setFont(self._fonts["light"])
        self.new_version_label.setAlignment(Qt.AlignCenter)
        
        main_layout.addStretch()
        main_layout.addWidget(self.update_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(48)
        main_layout.addWidget(self.info_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(48)
        main_layout.addStretch()
        main_layout.addWidget(self.new_version_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(48)


        self.setLayout(main_layout)

    def set_versions(self, new_version: str, new_version_date: str, old_version: Optional[str]):
        self.error_description.setText(f"Nova versão adquirida:<br/><b>{new_version}</b><br/>{new_version_date}<br/><br/>Versão anterior:<br/><b>{old_version if old_version else "Nenhuma"}</b>")
        self.error_description.adjustSize()
        self.adjustSize() 