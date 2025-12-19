# In a new file, e.g., src/services/delivery_service.py
import logging
from typing import Optional
import uuid
from PyQt5.QtCore import QThreadPool
from pydantic import ValidationError
from models.velide_delivery_models import Order
from models.cds_order_model import CdsOrder
from services.strategies.connectable_strategy import IConnectableStrategy
from config import ApiConfig
from workers.cds_logs_listener_worker import CdsLogsListenerWorker

class CdsStrategy(IConnectableStrategy):
    def __init__(self, api_config: ApiConfig, folder_to_watch: str):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._api_config = api_config
        self._folder_to_watch = folder_to_watch

        # These workers will be created and moved to threads later
        self._file_listener_worker = None
        self._thread_pool = QThreadPool.globalInstance() 

    def start_listening(self):
        """Creates and starts the file listener worker in a background thread."""
        self._file_listener_worker = CdsLogsListenerWorker(self._folder_to_watch)
        self._file_listener_worker.signals.new_order.connect(self._on_new_file_found)
        self._thread_pool.start(self._file_listener_worker)
    
    def stop_listening(self):
        if self._file_listener_worker is None:
            return
        self._file_listener_worker.stop()

    def requires_initial_configuration(self):
        return False
    
    def fetch_deliverymen(self, success, error):
        raise NotImplementedError
    
    def on_delivery_added(self, internal_id: str, external_id: str):
        return # Just ignore it.
    
    def on_delivery_failed(self, internal_id: Optional[float]):
        return # Just ignore it.

    def _on_new_file_found(self, delivery_data: dict):
        try: 
            cds_order: CdsOrder = CdsOrder.model_validate(delivery_data)
            normalized_order = self._transform(cds_order)
            self.order_normalized.emit(normalized_order)
        except ValidationError:
            self.logger.exception("Não foi possível adicionar uma entrega devido aos dados serem inválidos!")
            return
        except Exception:
            self.logger.exception("Ocorreu um erro inesperado ao processar uma nova entrega.")
            return

    def _transform(self, cds_order: CdsOrder) -> Order:
        """Transforms a source-specific CdsOrder into the common Order model."""
        splitted_address = cds_order.endereco.split(" - ")
        formatted_address = splitted_address[0]
        neighbourhood = None
        if len(splitted_address) == 6:
            neighbourhood = splitted_address[2]
        else: 
            self.logger.warning("O endereço está formatado incorretamente. Resultado final pode ser impreciso.")

        return Order(
            customerName=cds_order.nome_cliente,
            customerContact=cds_order.contato_cliente,
            address=formatted_address, 
            createdAt=cds_order.horario_pedido,
            address2=cds_order.complemento,
            reference=cds_order.referencia,
            neighbourhood=neighbourhood,
            # CDS doesn't provide an internal ID, so we generate one.
            internal_id=str(uuid.uuid4())
        )


