from typing import List, Any, Dict, Optional, Tuple
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt

from models.velide_delivery_models import DeliverymanResponse

class MappingTableModel(QAbstractTableModel):
    """
    A Qt model for managing a two-column mapping between a source and a destination.

    This model stores data as a list of lists (e.g., [['source1', 'dest1'], ...])
    and is designed to be used with a QTableView. It handles data access,
    editing, and notifying views of changes.
    """

    def __init__(self, parent: Optional[Any] = None) -> None:
        """Initializes the model."""
        super().__init__(parent)
        self._headers: List[str] = ["Fonte", "Destino"]
        self._data: List[Tuple[DeliverymanResponse, str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Returns the number of rows in the model."""
        return len(self._data)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """Returns the number of columns in the model."""
        return len(self._headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Optional[str]:
        """Returns the data for the given header section."""
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Optional[str]:
        """Returns the data at the given index for the specified role."""
        if not index.isValid():
            return None
        
        try:
            value = self._data[index.row()][index.column()]
        except IndexError:
            return None

        # Return the string value for display or editing
        if role in (Qt.DisplayRole, Qt.EditRole):
            if index.column() == 0:
                return value.name
            else:
                return value

        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        """
        Sets the data for the given index.

        This is called by the delegate when an editor's value is committed.
        """
        if role == Qt.EditRole and index.column() == 1:
            # The original code would fail here because tuples are immutable.
            # By using a list of lists, this assignment is now valid.
            self._data[index.row()][index.column()] = str(value)
            self.dataChanged.emit(index, index, [role])
            return True
        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        """Returns the item flags for the given index."""
        base_flags = super().flags(index)
        # Make the second column (index 1) editable.
        if index.column() == 1:
            base_flags |= Qt.ItemIsEditable
        return base_flags

    def load_data(self, data: List[Tuple[DeliverymanResponse, str]], headers: Optional[List[str]] = None) -> None:
        """
        Resets the model with new data.

        This method should be used to populate or update the entire table.
        It safely resets the model's internal state and notifies any connected
        views that the model has been drastically changed.
        """
        self.beginResetModel()
        self._data = data
        if headers:
            self._headers = headers
        self.endResetModel()

    def get_mappings(self) -> Dict[str, str]:
        """
        Returns the current mappings as a dictionary.

        Maps the source object's ID to the selected destination name.
        Only includes rows where a destination value has been selected.
        """
        return {
            source_obj.id: dest_name 
            for source_obj, dest_name in self._data 
            if dest_name
        }
