import asyncio
import logging
import httpx
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
from pydantic import ValidationError

from api.velide import Velide

# Importing all necessary models and exceptions from both original files
from models.velide_delivery_models import (
    Order,
    GraphQLRequestError,
    GraphQLParseError,
    GraphQLResponseError,
    # Add 'GraphQLError' here if it's a separate base exception you wish to catch
)

class VelideWorkerSignals(QObject):
    """
    Defines the signals available from the VelideWorker.
    
    Signals:
        delivery_added (dict): Emitted on successful delivery addition.
                               Payload is the response model's dictionary.
        deliverymen_retrieved (list): Emitted on successful retrieval of deliverymen.
                                      Payload is a List[DeliverymanResponse].
        error (str): Emitted when an error occurs. Payload is the error message.
        finished (): Emitted when the task is completed, successfully or not.
    """
    delivery_added = pyqtSignal(dict)
    delivery_deleted = pyqtSignal(str)
    deliverymen_retrieved = pyqtSignal(list)
    snapshot_retrieved = pyqtSignal(dict)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class VelideWorker(QRunnable):
    """
    Unified QRunnable worker to interact with the Velide API in a background thread.
    
    This worker can either add a delivery or retrieve deliverymen.
    Use the factory methods `.for_add_delivery()` or `.for_get_deliverymen()`
    to instantiate this worker for the desired task.
    """
    
    def __init__(self, velide: Velide, operation: str, **kwargs):
        """
        Private constructor. Please use the @classmethod factory methods.
        """
        super().__init__()
        self.signals = VelideWorkerSignals()
        self._velide = velide
        self._operation = operation  # 'add_delivery' or 'get_deliverymen'
        self._kwargs = kwargs      # Stores 'order' if required
        self.logger = logging.getLogger(__name__)

    @classmethod
    def for_add_delivery(cls, velide: Velide, order: Order) -> 'VelideWorker':
        """
        Creates a worker configured to add a new delivery.
        """
        return cls(velide, "add_delivery", order=order)

    @classmethod
    def for_delete_delivery(cls, velide: Velide, delivery_id: str) -> 'VelideWorker':
        """Creates a worker configured to delete a delivery."""
        return cls(velide, "delete_delivery", delivery_id=delivery_id)

    @classmethod
    def for_get_deliverymen(cls, velide: Velide) -> 'VelideWorker':
        """
        Creates a worker configured to fetch the list of deliverymen.
        """
        return cls(velide, "get_deliverymen")
    
    @classmethod
    def for_snapshot(cls, velide: Velide) -> 'VelideWorker':
        """Creates a worker to fetch the global delivery snapshot."""
        return cls(velide, "get_global_snapshot")

    def run(self):
        """
        The main work method. This is executed in the QThreadPool.
        
        It creates a new asyncio event loop to run the async API call
        and handles all potential errors, emitting the appropriate signals.
        """
        self.logger.debug(f"Iniciando tarefa Velide: {self._operation}...")
        
        try:
            # asyncio.run() creates, runs, and closes the event loop for us.
            asyncio.run(self._run_async())
            
        # --- Unified Exception Handling ---

        except ValidationError as e:
            error_message = f"Dados da operação inválidos ou incompletos.\n\nDetalhes: {e}"
            self.logger.error(f"Não foi possível executar '{self._operation}'. Dados inválidos: {e}")
            self.signals.error.emit(error_message)

        except GraphQLRequestError as e:
            error_message = f"Falha de comunicação com a API Velide (Código: {e.status_code}).\nVerifique sua conexão e credenciais."
            self.logger.error(f"Erro ao solicitar a Velide API: {e}")
            self.signals.error.emit(error_message)

        except GraphQLParseError as e:
            error_message = "A API Velide retornou uma resposta inesperada e ilegível. O problema pode ser temporário no servidor."
            self.logger.error(f"Não foi possível decodificar a Velide API. Resposta: {e.response_text}")
            self.signals.error.emit(error_message)

        except GraphQLResponseError as e:
            error_message = f"A API Velide recusou a operação com a seguinte mensagem:\n\n'{e}'"
            self.logger.error(f"Velide API recusou a operação: {e}")
            self.signals.error.emit(error_message)
            
        except httpx.RequestError as e:
            self.logger.exception("Erro de rede ao se comunicar com a Velide.")
            self.signals.error.emit(f"Erro de Rede: Não foi possível conectar à {e.request.url}")

        except Exception:
            self.logger.exception(f"Ocorreu uma falha inesperada durante a operação: {self._operation}.")
            # The traceback is logged, but a simpler message is sent to the UI.
            error_message = "Ocorreu um erro inesperado. Por favor, contate o suporte técnico."
            self.signals.error.emit(error_message)
            
        finally:
            self.signals.finished.emit()

    async def _run_async(self):
        """
        Asynchronous helper function to interact with the Velide client
        using its async context manager.
        
        It dispatches the correct API call based on self._operation and emits
        the appropriate success signals.
        """
        self.logger.debug("Entrando no contexto assíncrono do cliente Velide...")
        
        async with self._velide as client:
            
            if self._operation == "add_delivery":
                order = self._kwargs.get('order')
                if not order:
                    raise ValueError("Uma 'order' é necessária para a operação 'add_delivery'.")
                    
                response = await client.add_delivery(order)
                self.logger.info(f"Nova entrega adicionada: {response.location.properties.name}")
                self.signals.delivery_added.emit(response.model_dump())

            elif self._operation == "get_deliverymen":
                result = await client.get_deliverymen()
                self.logger.info(f"Busca de entregadores concluída. {len(result)} encontrados.")
                self.signals.deliverymen_retrieved.emit(result)

            elif self._operation == "delete_delivery":
                d_id = self._kwargs.get('delivery_id')
                success = await client.delete_delivery(d_id)
                if success:
                    self.logger.info(f"Entrega {d_id} deletada com sucesso.")
                    self.signals.delivery_deleted.emit(d_id)
                else:
                    raise Exception("A API retornou falha na deleção.")
                
            elif self._operation == "get_global_snapshot":
                # Call the new method we added to Velide class
                result_map = await client.get_active_deliveries_snapshot()
                self.signals.snapshot_retrieved.emit(result_map)
            
            else:
                raise NotImplementedError(f"Operação desconhecida do VelideWorker: {self._operation}")
