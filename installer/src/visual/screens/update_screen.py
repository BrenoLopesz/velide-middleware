from PyQt5.QtWidgets import QLabel, QWidget, QVBoxLayout
from visual.components.loading_icon import LoadingIcon
from visual.fonts import get_fonts
from PyQt5.QtCore import Qt, QPropertyAnimation, pyqtProperty, QTimer  # type: ignore[attr-defined]
import sys


class UpdateScreen(QWidget):
    def __init__(self):
        super().__init__()
        self._fonts = get_fonts()
        self._rotation = 0

        main_layout = QVBoxLayout()

        self.update_label = QLabel("Buscando Atualização...")
        self.update_label.setObjectName("title")
        self.update_label.setFont(self._fonts["bold"])
        self.update_label.setAlignment(Qt.AlignCenter)

        self.info_label = QLabel(
            "A aplicação irá iniciar em alguns segundos,<br/>por favor aguarde."
        )
        # self.info_label.setObjectName("title")
        self.info_label.setFont(self._fonts["light"])
        self.info_label.setAlignment(Qt.AlignCenter)

        self.new_version_label = QLabel("")
        self.new_version_label.setFont(self._fonts["light"])
        self.new_version_label.setAlignment(Qt.AlignCenter)

        self.add_loading_icon()

        main_layout.addStretch()
        main_layout.addWidget(self.update_label, alignment=Qt.AlignHCenter)
        main_layout.addWidget(self.new_version_label, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(64)
        main_layout.addWidget(self.loading_icon, alignment=Qt.AlignHCenter)
        main_layout.addSpacing(48)
        main_layout.addWidget(self.info_label, alignment=Qt.AlignHCenter)
        main_layout.addStretch()

        self.setLayout(main_layout)

    @pyqtProperty(int) # type: ignore[type-var]
    def rotation(self):
        return self._rotation

    @rotation.setter # type: ignore[type-var]
    def set_rotation(self, rotation):
        self._rotation = rotation
        self.valueChanged.emit(rotation)

    def add_loading_icon(self):
        self.loading_icon = LoadingIcon()
        self.loading_icon.move(
            round(300 - self.loading_icon.width() / 2),
            round(300 - self.loading_icon.height() / 2),
        )
        self.anim = QPropertyAnimation(self.loading_icon, b"rotation")
        self.anim.setDuration(1600)
        self.anim.setStartValue(0)
        self.anim.setEndValue(360)
        self.anim.setLoopCount(-1)
        self.anim.start()

    def finish(self, new_version, new_version_date, old_version):
        self.update_label.setText("Atualização concluída.")

        self.new_version_label.setText(
            (
                "Nova versão adquirida:<br/><b>{}</b><br/>{}<br/><br/>"
                "Versão anterior:<br/><b>{}</b>"
            ).format(
                new_version,
                new_version_date,
                "Nenhuma" if old_version is None or old_version == "" else old_version,
            )
        )
        self.new_version_label.setFont(self._fonts["regular"])
        self.new_version_label.setAlignment(Qt.AlignCenter)
        self.new_version_label.setFixedWidth(600)
        self.new_version_label.move(0, 234)
        self.new_version_label.adjustSize()

        self.info_label.setText("A aplicação iniciará agora.")
        # self.info_label.setObjectName("title")
        self.info_label.setFont(self._fonts["regular"])
        self.info_label.move(0, 506)

        self.image.setParent(None)

        self.timer = QTimer()
        self.timer.timeout.connect(lambda: sys.exit(0))
        self.timer.setParent(self.parent)
        self.timer.start(5000)

    def set_status_text(self, text: str):
        self.update_label.setText(text)
        self.update_label.adjustSize()

    def set_version_text(self, text: str):
        self.new_version_label.setText(text)
        self.new_version_label.adjustSize()
