from PyQt5.QtWidgets import QTableView, QAbstractItemView, QHeaderView, QToolTip, QStyledItemDelegate

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QFontMetrics
from models.velide_delivery_models import Order
from models.delivery_table_model import DeliveryRowStatus, DeliveryTableModel
from visual.fonts import get_fonts

class ElidingTooltipDelegate(QStyledItemDelegate):
    """
    A delegate that shows a tooltip for an item if the text
    is truncated or elided ('...').
    """
    def helpEvent(self, event, view, option, index):
        """
        This event is triggered when a tooltip is about to be shown.
        """
        # We only handle ToolTip events
        if event.type() != QEvent.ToolTip:
            return super().helpEvent(event, view, option, index)

        # Get the full text for the item
        full_text = index.model().data(index, Qt.DisplayRole)
        if not full_text:
            return False # Nothing to show

        # Get the font metrics to calculate text width
        font = view.font()
        font_metrics = QFontMetrics(font)
        
        # Calculate the required width for the full text
        required_width = font_metrics.horizontalAdvance(full_text)

        # Get the available width in the cell's rectangle
        # We subtract a small margin for better visual appearance
        available_width = option.rect.width() - 5 

        # If the required width is greater than what's available, show the tooltip
        if required_width > available_width:
            # QToolTip.showText() shows the tooltip
            # event.globalPos() gets the current mouse cursor position on the screen
            QToolTip.showText(event.globalPos(), full_text, view)
            return True # We handled the event
        else:
            # If text fits, hide any existing tooltip and do nothing
            QToolTip.hideText()
        
        return super().helpEvent(event, view, option, index)

class DeliveriesTable(QTableView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fonts = get_fonts()
        self._model = DeliveryTableModel()
        self.configure_table()

    def configure_table(self):
        """Sets up the visual properties and behavior of the table."""
        self.setModel(self._model)

        # Disables editting
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

        h_header = self.horizontalHeader()

        # Changes font
        self.setFont(self.fonts["regular_small"])
        h_header.setFont(self.fonts["regular_small"])

        # Enables horizontal scroll
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        h_header.setSectionResizeMode(QHeaderView.Interactive) 

        # Column 0 ("Horário"): Resize to content, with a minimum width
        h_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.setColumnWidth(0, 150) # A good starting width, ResizeToContents can still expand it
        
        # Column 1 ("Status"): Resize to content
        h_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)

        # Column 2 ("Endereço"): Takes all remaining space
        h_header.setSectionResizeMode(2, QHeaderView.Stretch)

        # Create and apply the custom delegate ONLY to the column that needs it
        self.setItemDelegateForColumn(2, ElidingTooltipDelegate(self))

        # Optional: Hide the vertical header (the row numbers on the left)
        self.verticalHeader().setVisible(False)

    def add_delivery(self, delivery_id: str, order: Order):
        """Public method to add a new acknowledged delivery."""
        self._model.add_delivery_acknowledge(delivery_id, order)

    def update_delivery(self, delivery_id: str, order: Order, new_status: DeliveryRowStatus):
        """Public method to update the status of an existing delivery."""
        self._model.update_delivery(delivery_id, order, new_status)