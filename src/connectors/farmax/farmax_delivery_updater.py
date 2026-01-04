import logging
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_worker import FarmaxWorker
from models.velide_delivery_models import Order
from services.deliverymen_retriever_service import DeliverymenRetrieverService

class FarmaxDeliveryUpdater(QObject):
    """
    Responsible for pushing status updates (Write operations) from the System 
    back to the Farmax ERP.
    """
    
    # Signals to notify the Strategy of completion/failure (optional, but good for logging)
    update_success = pyqtSignal(str, str) # internal_id, operation
    update_failed = pyqtSignal(str, str, str) # internal_id, operation, error_message

    def __init__(self, repository: FarmaxRepository, deliverymen_retriever: DeliverymenRetrieverService):
        super().__init__()
        self._logger = logging.getLogger(__name__)
        self._repository = repository
        self._thread_pool = QThreadPool.globalInstance()
        self._deliverymen_retriever = deliverymen_retriever

    def mark_as_in_route(self, order: Order, deliveryman_external_id: str) -> None:
        """
        Updates the ERP to indicate the delivery has left (In Route).
        """
        # Simple extraction of primitives
        sale_id = float(order.internal_id)
        driver = self._deliverymen_retriever.get_deliveryman_by_external_id(deliveryman_external_id)
        if not driver:
            self._logger.error(f"Não foi possível definir uma entrega ({order.internal_id}) como rota iniciada! Entregador não foi encontrado.")
            return
        current_time = datetime.now().time()

        # No Pydantic construction needed!
        worker = FarmaxWorker.for_update_delivery_as_in_route(
            self._repository,
            sale_id=sale_id,
            driver_id=float(driver.id),
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