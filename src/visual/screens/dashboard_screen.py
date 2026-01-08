from typing import Optional
from PyQt5.QtWidgets import QWidget, QVBoxLayout
from visual.components.deliveries_table import DeliveriesTable
from visual.components.log_table import LogTable

class DashboardScreen(QWidget):
    def __init__(self) -> None:
        super().__init__()
        
        # 1. Internal state storage
        self._is_footer_enabled: bool = False
        self.footer: Optional[QWidget] = None

        self.main_layout = QVBoxLayout(self)

        self.log_table = LogTable()
        self.deliveries_table = DeliveriesTable()

        self.main_layout.addWidget(self.deliveries_table)
        self.main_layout.addSpacing(12)
        self.main_layout.addWidget(self.log_table)

        self.setLayout(self.main_layout)

    @property
    def footer_enabled(self) -> bool:
        """Read-only access to the current state."""
        return self._is_footer_enabled

    @footer_enabled.setter
    def footer_enabled(self, enabled: bool):
        """
        Sets the state. Triggers UI updates only if the value actually changes.
        Usage: my_dashboard.footer_enabled = True
        """
        if self._is_footer_enabled == enabled:
            return

        self._is_footer_enabled = enabled
        self._update_footer_visibility()

    def _update_footer_visibility(self):
        """Internal method to handle the UI logic for the footer."""
        if self._is_footer_enabled:
            # 2. Lazy Import and Instantiation
            # The import happens only the first time this is set to True.
            if self.footer is None:
                from visual.components.dashboard_footer import DashboardFooter
                self.footer = DashboardFooter()

            # Add to layout and show
            self.main_layout.addWidget(self.footer)
            self.footer.show()
            
        else:
            # 3. Clean Removal
            if self.footer:
                # hide() makes it invisible; removeWidget() releases the layout space
                self.footer.hide()
                self.main_layout.removeWidget(self.footer)