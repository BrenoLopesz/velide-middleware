from typing import List, Any, Dict, Optional
from PyQt5.QtWidgets import QTableView, QComboBox, QStyledItemDelegate, QHeaderView
from PyQt5.QtCore import Qt

from models.base_models import BaseLocalDeliveryman
from models.mapping_table_model import MappingTableModel
from models.velide_delivery_models import DeliverymanResponse
from visual.fonts import get_fonts

class ComboBoxDelegate(QStyledItemDelegate):
    """
    A delegate that provides a searchable QComboBox as the editor for a table view cell.
    """
    def __init__(self, options: List[str], parent: Optional[Any] = None) -> None:
        """Initializes the delegate with a list of options for the combo box."""
        super().__init__(parent)
        self.options = options

    def createEditor(self, parent: Any, option: Any, index: Any) -> QComboBox:
        """Creates the QComboBox editor when a cell is double-clicked."""
        combo = QComboBox(parent)
        self.update_options(combo) # Use a helper to set options

        return combo
    
    def setEditorData(self, editor: QComboBox, index: Any) -> None:
        """Sets the editor's current value from the model's data."""
        value = index.model().data(index, Qt.EditRole)
        editor.setCurrentText(str(value))

    def setModelData(self, editor: QComboBox, model: Any, index: Any) -> None:
        """Saves the editor's current value back to the model."""
        value = editor.currentText()
        model.setData(index, value, Qt.EditRole)

    def update_options(self, editor: Optional[QComboBox] = None) -> None:
        """Helper to update options in a combo box or for the delegate itself."""
        if editor is not None:
            editor.clear()
            editor.addItems(self.options)


class MappingTableView(QTableView):
    """
    A professional, reusable QTableView for displaying and editing
    a source-to-destination mapping.

    It uses a custom model for data management and a custom delegate
    to provide a searchable QComboBox for the destination column.
    """
    def __init__(self, parent: Optional[Any] = None) -> None:
        """Initializes the view and its components."""
        super().__init__(parent)
        self._fonts = get_fonts() 
        self._model = MappingTableModel(self)
        self._delegate = ComboBoxDelegate([], self) # Start with empty options
        self._configure_view()

    def _configure_view(self) -> None:
        """Sets up the visual properties and behavior of the table view."""
        self.setModel(self._model)
        self.setItemDelegateForColumn(1, self._delegate)
        
        self.setFont(self._fonts["regular_small"])

        # Configure Headers
        header = self.horizontalHeader()
        # First column resizes to fit its content
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        # Second (last) column stretches to fill the remaining space
        header.setSectionResizeMode(1, QHeaderView.Stretch)

        self.setAlternatingRowColors(True)
        # Vertical scrollbar will appear automatically when content exceeds viewport
        self.setVerticalScrollMode(self.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def populate_table(
        self,
        source_items: List[DeliverymanResponse],
        destination_options: List[BaseLocalDeliveryman],
        default_mappings: Optional[Dict[str, str]] = None,
        headers: Optional[List[str]] = None
    ) -> None:
        """
        Populates the table with data. This is the primary method for setting/updating content.

        Args:
            source_items: A list of DeliverymanResponse objects for the first column.
            destination_options: A list of FarmaxDeliveryman objects to use as options.
            default_mappings: An optional dictionary mapping source item IDs (DeliverymanResponse.id)
                            to their default destination name (FarmaxDeliveryman.name).
            headers: An optional list of two strings to set as column headers.
        """
        # Ensure default_mappings is a dictionary to prevent errors
        mappings = default_mappings or {}

        # Prepare data in the format the model requires: List[List[Any]]
        # The source column (col 0) will hold the full source object
        # The destination column (col 1) will hold the mapped string name
        table_data = [
            [source, mappings.get(source.id, "")] for source in source_items
        ]

        # Extract the names from the destination objects
        dest_names = [option.name for option in destination_options]

        # Update the delegate with the new set of options for the dropdown
        self._delegate.options = sorted(dest_names) # Add a blank option

        # Load the prepared data into the model
        self._model.load_data(table_data, headers)

        # # After loading data, make the editors in column 1 persistent
        for row in range(self._model.rowCount()):
            index_to_edit = self._model.index(row, 1)  # Get the index for column 1
            self.openPersistentEditor(index_to_edit)

    def get_mappings(self) -> Dict[str, str]:
        """Convenience method to retrieve the current mappings from the model."""
        return self._model.get_mappings()
