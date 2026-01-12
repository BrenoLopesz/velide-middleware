from typing import Optional
from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtWidgets import QSystemTrayIcon, QWidget

class MinimizeToTrayFilter(QObject):
    """
    An event filter that intercepts the Close event of a window.
    Instead of closing, it hides the window and shows a notification.
    """
    def __init__(
            self, 
            tray_icon: QSystemTrayIcon, 
            parent: Optional[QObject] = None
        ) -> None:
        super().__init__(parent)
        self.tray_icon = tray_icon
        self.notification_shown = False # Optional: To show notification only once

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Check if the event is a Window Close request
        if event.type() == QEvent.Close:
            # check if the event is "spontaneous" (triggered by User clicking X)
            # If we trigger close() programmatically (e.g. via Quit menu), 
            # spontaneous is False.
            if event.spontaneous():
                
                # 1. Perform the Logic: Hide instead of Close
                if isinstance(obj, QWidget):
                    obj.hide()

                # 2. Show Notification
                self.tray_icon.showMessage(
                    "Aplicação em Execução",
                    "A integração continua rodando em segundo plano.\n"
                    "Clique no ícone para abrir novamente.",
                    QSystemTrayIcon.Information,
                    2000
                )
                
                # 3. Return True to indicate we have "consumed"/handled the event.
                # The View will NEVER receive this close event, so it won't close.
                return True

        # For all other events (Mouse, Resize, or programmatic Close), 
        # let standard processing continue.
        return super().eventFilter(obj, event)