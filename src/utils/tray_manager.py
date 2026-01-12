import logging
from typing import Callable
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction, QStyle, QWidget
from PyQt5.QtGui import QIcon

class AppTrayIcon(QSystemTrayIcon):
    """
    A reusable System Tray Icon class for PyQt5 applications.
    
    Args:
        icon_path (str): Path to the icon image file.
        parent (QWidget): The parent widget (usually the MainWindow).
        callback_open_ui (callable): Function to call when 'Open UI' is clicked 
                                    or icon is double-clicked.
        callback_close_app (callable): Function to call when 'Close' is clicked.
        tooltip (str): Text to show when hovering over the icon.
    """
    def __init__(
        self, 
        icon_path: str, 
        parent: QWidget, 
        callback_open_ui: Callable[[], None], 
        callback_close_app: Callable[[], None], 
        tooltip: str = "My App"
    ) -> None:
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        
        # Store dependencies
        self.callback_open_ui = callback_open_ui
        self.callback_close_app = callback_close_app
        
        # 1. Setup Icon (with fallback)
        self._set_icon_safe(icon_path)
        
        # 2. Setup Tooltip
        self.setToolTip(tooltip)
        
        # 3. Setup Menu
        self._setup_menu()
        
        # 4. Handle Activation (Double-click behavior)
        self.activated.connect(self._on_activated)
        
        # 5. Show the icon immediately
        self.show()

    def _set_icon_safe(self, icon_path: str) -> None:
        """Attempts to load the user icon; falls back to system icon on failure."""
        icon = QIcon(icon_path)
        
        # Check if icon loaded correctly (isNull returns True if file invalid/missing)
        if icon.isNull():
            self.logger.warning(
                f"Não foi possível carregar o ícone em '{icon_path}'. "
                "Usando padrão do sistema."
            )
            # Fallback to a standard generic system icon
            
            icon = self.parent().style().standardIcon( # type: ignore[attr-defined]
                QStyle.SP_ComputerIcon
            )
        self.setIcon(icon)

    def _setup_menu(self) -> None:
        """Creates the context menu with injected callbacks."""
        menu = QMenu(parent=self.parent()) # type: ignore[call-overload]

        # Option 1: Open UI
        action_open = QAction("Abrir", self.parent())
        action_open.triggered.connect(self.callback_open_ui)
        menu.addAction(action_open)

        # Separator
        menu.addSeparator()

        # Option 2: Close
        action_close = QAction("Desativar", self.parent())
        action_close.triggered.connect(self.callback_close_app)
        menu.addAction(action_close)

        self.setContextMenu(menu)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handles clicks on the tray icon itself."""
        # Open UI on Left Click (Trigger) or Double Click
        if reason == QSystemTrayIcon.Trigger or reason == QSystemTrayIcon.DoubleClick:
            self.callback_open_ui()

    def cleanup(self) -> None:
        """
        Manually hides the icon. Call this before app exit to prevent ghost icons.
        """
        self.hide()