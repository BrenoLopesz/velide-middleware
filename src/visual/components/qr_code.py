import io
from PyQt5.QtWidgets import QWidget, QLabel, QVBoxLayout
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
import qrcode

class QRCode(QWidget):
    def __init__(self, url: str, size: int = 128):
        super().__init__()
        self.url = url
        self._size = size
        
        layout = QVBoxLayout()
        # No margins so the QR code fills the widget
        layout.setContentsMargins(0, 0, 0, 0) 
        
        # --- Create and configure the label ---
        self.qr_image_label = QLabel() # Parent is no longer needed here
        self.qr_image_label.setAlignment(Qt.AlignCenter)
        self.qr_image_label.setFixedSize(self._size, self._size) 
        self.qr_image_label.setStyleSheet("border: 2px solid grey; border-radius: 8px; background-color: #fff")
        
        # --- Add the label to the layout ---
        layout.addWidget(self.qr_image_label)
        
        # --- Set the layout on this QRCode widget ---
        self.setLayout(layout)
        self.build()

    def build(self):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(self.url)
        qr.make(fit=True)

        # Create an image from the QR Code instance
        img = qr.make_image(fill_color="black", back_color="white")

        # 1. Convert the image to RGBA (Red, Green, Blue, Alpha)
        img = img.convert("RGBA")

        # 2. Get the image data
        datas = img.getdata()

        # 3. Process the data to make white pixels transparent
        newData = []
        for item in datas:
            # If the pixel is white (R, G, B values of 255)
            if item[0] == 255 and item[1] == 255 and item[2] == 255:
                # Replace it with a transparent pixel (R, G, B, Alpha)
                newData.append((255, 255, 255, 0))
            else:
                # Keep other pixels (the black parts) opaque
                newData.append(item)

        img.putdata(newData)
        # --- 2. Convert the Pillow image to a QPixmap ---
        # Save the image to an in-memory byte buffer
        buffer = io.BytesIO()
        img.save(buffer, "PNG")
        
        # Create a QPixmap and load the image data from the buffer
        qt_pixmap = QPixmap()
        qt_pixmap.loadFromData(buffer.getvalue(), "PNG")

        # --- 3. Display the QPixmap in the QLabel ---
        # Scale the pixmap to fit the label while keeping aspect ratio
        scaled_pixmap = qt_pixmap.scaled(
            self.qr_image_label.size(),
            Qt.KeepAspectRatio, 
            Qt.SmoothTransformation
        )
        self.qr_image_label.setPixmap(scaled_pixmap)