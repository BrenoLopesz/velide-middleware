import logging
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
from models.velide_delivery_models import Order

class FarmaxDeliveryUpdater(QObject):
    """
    Responsible for pushing status updates (Write operations) from the System 
    back to the Farmax ERP.
    """
    
    # Signals to notify the Strategy of completion/failure (optional, but good for logging)
    update_success = pyqtSignal(str, str) # internal_id, operation
    update_failed = pyqtSignal(str, str, str) # internal_id, operation, error_message

    def __init__(self, repository: FarmaxRepository):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._repository = repository
        self._thread_pool = QThreadPool.globalInstance()

    def mark_as_in_route(self, order: Order) -> None:
        """
        Updates the ERP to indicate the delivery has left (In Route).
        """
        # Validation: We need a driver to assign the delivery in Farmax
        if not order.driver or not order.driver.external_id:
            msg = f"Impossível atualizar pedido {order.internal_id}: Motorista sem ID."
            self._logger.warning(msg)
            self.update_failed.emit(str(order.internal_id), "IN_ROUTE", msg)
            return

        # Simple extraction of primitives
        sale_id = float(order.internal_id)
        driver_id = str(order.driver.external_id)
        current_time = datetime.now().time()

        # No Pydantic construction needed!
        worker = FarmaxWorker.for_update_delivery_as_in_route(
            self._repository,
            sale_id=sale_id,
            driver_id=driver_id,
            left_at=current_time
        )

        worker.signals.success.connect(lambda: self._on_success(order.internal_id, "IN_ROUTE"))
        worker.signals.error.connect(lambda err: self._on_error(order.internal_id, "IN_ROUTE", err))
        
        self._thread_pool.start(worker)

    def mark_as_done(self, order: Order) -> None:
        """
        Updates the ERP to indicate the delivery is finished (Done).
        """
        sale_id = float(order.internal_id)
        current_time = datetime.now().time()

        worker = FarmaxWorker.for_update_delivery_as_done(
            self._repository,
            sale_id=sale_id,
            ended_at=current_time
        )

        worker.signals.success.connect(lambda: self._on_success(order.internal_id, "DONE"))
        worker.signals.error.connect(lambda err: self._on_error(order.internal_id, "DONE", err))

        self._thread_pool.start(worker)

    def _on_success(self, internal_id: str, operation: str):
        self._logger.debug(f"Sucesso ao atualizar Farmax: Pedido {internal_id} -> {operation}")
        self.update_success.emit(internal_id, operation)

    def _on_error(self, internal_id: str, operation: str, error: str):
        self._logger.error(f"Erro ao atualizar Farmax ({operation}) Pedido {internal_id}: {error}")
        self.update_failed.emit(internal_id, operation, error)