# models/delivery_table_model.py

from enum import Enum
from typing import Dict, List
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QColor, QBrush
from pydantic import BaseModel, Field

from api.sqlite_manager import DeliveryStatus
from models.velide_delivery_models import Order

class DeliveryIdNotFoundError(LookupError):
    """Exception raised when a delivery ID is not found in the model."""
    def __init__(self, delivery_id: str):
        self.delivery_id = delivery_id
        message = f"Entrega com ID '{self.delivery_id}' não encontrado."
        super().__init__(message)

class DeliveryRowStatus(Enum):
    UNDEFINED = "Indefinido"
    ACKNOWLEDGE = "Reconhecido"
    SENDING = "Enviando..."
    ADDED = "Adicionado"
    IN_PROGRESS = "Em Rota"
    DELIVERED = "Entregue"
    DELETING = "Deletando..."
    CANCELLED = "Cancelado"
    ERROR = "Erro"

STATUS_COLORS = {
    DeliveryRowStatus.UNDEFINED: None,
    DeliveryRowStatus.ACKNOWLEDGE: None,
    DeliveryRowStatus.SENDING: QBrush(QColor(0, 152, 210)),
    DeliveryRowStatus.ADDED: QBrush(QColor(74, 160, 53)),
    DeliveryRowStatus.IN_PROGRESS: QBrush(QColor(5, 129, 206)),
    DeliveryRowStatus.DELIVERED: QBrush(QColor(13, 84, 43)),
    DeliveryRowStatus.ERROR: QBrush(QColor(231, 98, 104)), 
    DeliveryRowStatus.DELETING: QBrush(QColor(233, 149, 33)),
    DeliveryRowStatus.CANCELLED: QBrush(QColor(211, 118, 0)),
}

# This serves as the single source of truth for converting Backend -> UI.
DB_TO_UI_MAP = {
    # Backend (SQLite)          # UI (Table Row)
    DeliveryStatus.PENDING:     DeliveryRowStatus.SENDING,
    DeliveryStatus.SENDING:     DeliveryRowStatus.SENDING,
    DeliveryStatus.ADDED:       DeliveryRowStatus.ADDED,
    DeliveryStatus.IN_PROGRESS: DeliveryRowStatus.IN_PROGRESS,
    
    # Terminal States (Mapped for completeness/live updates)
    DeliveryStatus.DELIVERED:   DeliveryRowStatus.ADDED, 
    DeliveryStatus.CANCELLED:   DeliveryRowStatus.CANCELLED,
    DeliveryStatus.FAILED:      DeliveryRowStatus.ERROR,
}

def map_db_status_to_ui(db_status: DeliveryStatus) -> DeliveryRowStatus:
    """Safe retrieval wrapper with a fallback."""
    return DB_TO_UI_MAP.get(db_status, DeliveryRowStatus.UNDEFINED)

class DeliveryRowModel(BaseModel):
    id: str = Field(..., description="Unique ID related to this delivery.")
    status: DeliveryRowStatus = Field(default=DeliveryRowStatus.UNDEFINED, description="Delivery status.")
    order: Order = Field(..., description="Original order details.")

    def get_formatted_created_at(self) -> str:
        return self.order.created_at.strftime("%d/%m %H:%M:%S")

class DeliveryTableModel(QAbstractTableModel):
    """
    A model to hold delivery data for the QTableView.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._headers = ["Horário", "Status", "Endereço"]
        self._data: List[DeliveryRowModel] = []
        self._id_map: Dict[str, int] = {} # For fast lookups

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._headers)
    
    def _font_data(self, index):
        row = index.row()
        col = index.column()

        if col != 1:
            return None
        
        item = self._data[row]
        status = item.status
        
        return STATUS_COLORS[status]


    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        
        if role == Qt.ForegroundRole:
            return self._font_data(index)

        if role != Qt.DisplayRole:
            return None
        
        
        row = index.row()
        col = index.column()
        item = self._data[row]

        if col == 0:
            return item.get_formatted_created_at()
        elif col == 1:
            return item.status.value
        elif col == 2:
            return item.order.address
        
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._headers[section]
        return None

    def add_delivery_acknowledge(self, id: str, order_data: Order):
        """Adds a new delivery to the model with 'Reconhecido' status."""
        row_position = self.rowCount()
        self.beginInsertRows(QModelIndex(), row_position, row_position)
        
        new_entry = DeliveryRowModel(
            id=id,
            order=order_data,
            status=DeliveryRowStatus.ACKNOWLEDGE,
        )
        
        self._data.append(new_entry)        
        self._id_map[new_entry.id] = row_position 

        self.endInsertRows()

    def update_delivery(self, id: str, order: Order, new_status: DeliveryRowStatus):
        """
        Finds a delivery by its ID and updates its status and order data.

        Raises:
            DeliveryIdNotFoundError: If the provided ID does not exist in the model.
        """
        if id not in self._id_map:
            raise DeliveryIdNotFoundError(delivery_id=id)

        row_index = self._id_map[id]
        
        # Modify the object IN-PLACE. This actually works.
        self._data[row_index].status = new_status
        self._data[row_index].order = order
        
        # Emit signal to tell the view what actually changed
        start_index = self.index(row_index, 0)
        end_index = self.index(row_index, self.columnCount() - 1)
        self.dataChanged.emit(start_index, end_index)