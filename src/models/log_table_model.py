# models/log_table_model.py

from typing import Any, List, Optional
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QBrush, QColor
from pydantic import BaseModel, Field

STATUS_COLORS = {
    "Info": QBrush(QColor(0, 152, 210)),
    "Crítico": QBrush(QColor(78, 29, 31)),
    "Erro": QBrush(QColor(231, 98, 104)),
    "Aviso": QBrush(QColor(211, 118, 0)),
}

class LogRowModel(BaseModel):
    """
    Represents a single row of data in the log table.
    """
    timestamp: str = Field(..., description="The formatted timestamp of the log entry.")
    level: str = Field(..., description="The log level (e.g., INFO, ERROR).")
    message: str = Field(..., description="The log message content.")

class LogTableModel(QAbstractTableModel):
    """
    A model to hold log data for the LogTable QTableView.
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._headers = ["Horário", "Tipo", "Mensagem"]
        self._data: List[LogRowModel] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._data)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._headers)

    def _font_data(self, index) -> Optional[QBrush]:
        """
        Provides a hook for custom font/color styling based on data.
        Currently returns None, but can be expanded to, for example,
        color rows based on the log level (e.g., ERROR in red).
        """
        row = index.row()
        col = index.column()

        if col != 1: 
            return None

        item = self._data[row]
        return STATUS_COLORS.get(item.level, None)

    def data(self, index, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None

        # Handle font/color role, as requested
        if role == Qt.ForegroundRole:
            return self._font_data(index)
            
        if role != Qt.DisplayRole:
            return None
        
        row = index.row()
        col = index.column()
        item = self._data[row]

        if col == 0:
            return item.timestamp
        elif col == 1:
            return item.level
        elif col == 2:
            return item.message
        
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole) -> Any:
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._headers[section]
        return None

    def add_log_entry(self, timestamp: str, level: str, message: str) -> None:
        """Adds a new log entry to the end of the model."""
        row_position = self.rowCount()
        self.beginInsertRows(QModelIndex(), row_position, row_position)
        
        new_entry = LogRowModel(
            timestamp=timestamp,
            level=level,
            message=message
        )
        
        self._data.append(new_entry)
        
        self.endInsertRows()