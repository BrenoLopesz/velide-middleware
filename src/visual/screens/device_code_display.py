from PyQt5.QtWidgets import QLabel, QWidget, QVBoxLayout
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from utils.device_code import DeviceCodeDict
from visual.fonts import get_fonts
from visual.components.qr_code import QRCode

class DeviceCodeDisplay(QWidget):
    expired = pyqtSignal()

    def __init__(self, device_code: DeviceCodeDict):
        super().__init__()
        self.fonts = get_fonts()
        self.device_code = device_code

        self.main_layout = QVBoxLayout()
        
        
        self.qr_cta_label = QLabel("Escaneie o <b>QR Code</b>")
        self.qr_cta_label.setFont(self.fonts["regular"])
        self.qr_cta_label.setAlignment(Qt.AlignCenter)
        
        self.link_cta_label = QLabel("Ou acesse")
        self.link_cta_label.setFont(self.fonts["light"])
        self.link_cta_label.setStyleSheet("font-size: 10pt;")
        self.link_cta_label.setAlignment(Qt.AlignCenter)

        self.code_label = QLabel("Código")
        self.code_label.setStyleSheet("font-size: 10pt;")
        self.code_label.setFont(self.fonts["light"])

        self.code_display = QLabel(device_code["user_code"])
        self.code_display.setFont(self.fonts["bold"])
        self.code_display.setObjectName("codeDisplay")
        self.code_display.setAlignment(Qt.AlignCenter)

        self.login_link = QLabel('<a href=\"{}\" style=\"color: #0EA5E9\">{}</a>'.format(device_code["verification_uri_complete"], device_code["verification_uri"]))
        self.login_link.setTextFormat(Qt.RichText) 
        self.login_link.setObjectName("link")
        self.login_link.setFont(self.fonts["regular"])
        self.login_link.setOpenExternalLinks(True)
        self.login_link.setAlignment(Qt.AlignCenter)

        self.qr_code = QRCode(device_code["verification_uri_complete"], 152)

        self.add_expiration_timer()

        self.main_layout.setSpacing(0)
        
        self.main_layout.addStretch()
        self.main_layout.addWidget(self.qr_cta_label, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(12)
        self.main_layout.addWidget(self.qr_code, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(12)
        self.main_layout.addWidget(self.code_label, alignment=Qt.AlignHCenter)
        self.main_layout.addWidget(self.code_display, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(16)
        self.main_layout.addWidget(self.link_cta_label, alignment=Qt.AlignHCenter)
        self.main_layout.addWidget(self.login_link, alignment=Qt.AlignHCenter)
        self.main_layout.addSpacing(42)
        self.main_layout.addWidget(self.expire_label, alignment=Qt.AlignHCenter)

        self.setLayout(self.main_layout)

    def add_expiration_timer(self):
        self.remaining_time = self.device_code["expires_in"]

        # Used to update display (updates every second)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_timer)
        self.timer.start(1000)

        self.expire_label = QLabel("Expira em ----:----")
        self.expire_label.setFont(self.fonts["light"])
        self.expire_label.setStyleSheet("font-size: 10pt;")
        self.expire_label.setAlignment(Qt.AlignCenter)
        self.expire_label.update()

    def update_timer(self):
        if self.remaining_time > 0:
            minutes = self.remaining_time // 60
            seconds = self.remaining_time % 60
            self.expire_label.setText("Expira em {:02d}:{:02d}".format(minutes, seconds))
            self.remaining_time -= 1
        else:
            self.expire_label.setText("Código expirado.")
            self.expire_label.setObjectName("error")
            self.timer.stop()
            self.expired.emit()