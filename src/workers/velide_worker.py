import asyncio
import logging
import traceback
from PyQt5.QtCore import QObject, pyqtSignal
from pydantic import ValidationError
from api.velide import Velide
# Assuming the custom exceptions from the Velide class are accessible for more specific error handling
from models.velide_delivery_models import (
    GraphQLRequestError,
    GraphQLParseError,
    GraphQLResponseError,
    Order
)


class VelideWorker(QObject):
    """
    A QObject worker to interact with the Velide API in a background thread.
    """
    delivery_added = pyqtSignal(dict)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, velide_api: Velide, order: Order):
        """
        Initialize the worker.
        
        Args:
            velide_api: An initialized instance of the async Velide API client.
        """
        super().__init__()
        self._velide_api = velide_api
        self._order = order
        self.logger = logging.getLogger(__name__)

    def run(self):
        """
        Slot to add a delivery. This method runs an async task and waits for it to complete.
        It's designed to be run in a separate QThread to avoid blocking the GUI.
        
        Args:
            delivery_details: A dictionary containing the order information.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # 2. Run your async function using loop.run_until_complete()
            #    This runs the task but does NOT close the loop afterwards.
            response = loop.run_until_complete(self._add_delivery_async(self._order))
            
            self.logger.info(f"Nova entrega adicionada: {response.location.properties.name}")
            self.delivery_added.emit(response.model_dump())

        except ValidationError as e:
            # Validation error: the data sent to the worker is incorrect or incomplete.
            # This is a pre-request check, caught before any network call is made.
            error_message = f"Dados da entrega inválidos ou incompletos. Por favor, verifique as informações fornecidas.\n\nDetalhes: {e}"
            self.logger.error(f"Não foi possível adicionar uma entrega. Dados de entrega inválidos ou incompletos: {e}")
            self.error.emit(error_message)

        except GraphQLRequestError as e:
            # Request error: failure in HTTP communication (e.g., no internet, 401, 403, 500).
            error_message = f"Falha de comunicação com a API Velide (Código: {e.status_code}).\nVerifique sua conexão com a internet e se as credenciais de acesso estão corretas."
            self.logger.error(f"Erro ao solicitar a Velide API: {e}")
            self.error.emit(error_message)

        except GraphQLParseError as e:
            # Parsing error: the API returned something that is not valid JSON.
            # This usually indicates a serious problem on the API server itself.
            error_message = "A API Velide retornou uma resposta inesperada e ilegível. O problema pode ser temporário no servidor."
            self.logger.error(f"Não foi possível decodificar a Velide API. Resposta: {e.response_text}")
            self.error.emit(error_message)

        except GraphQLResponseError as e:
            # GraphQL response error: The request was successful, but the API returned
            # a logical or business-related error (e.g., "customer not found", "invalid address").
            error_message = f"A API Velide recusou a entrega com a seguinte mensagem:\n\n'{e}'"
            self.logger.error(f"Velide API recusou a entrega: {e}")
            self.error.emit(error_message)
        except Exception:
            # Generic and unexpected error: catches any other exception as a fallback.
            self.logger.exception("Ocorreu uma falha inesperada ao adicionar a entrega.")
            error_message = f"Ocorreu um erro inesperado. Por favor, contate o suporte técnico com os detalhes abaixo:\n{traceback.format_exc()}"
            self.error.emit(error_message)
        finally:
            loop.close()
            self.finished.emit()

    async def _add_delivery_async(self, order: Order):
        """
        Asynchronous helper function to perform the actual API call.
        """
        # This 'async with' block will call __aenter__ on entry
        # and __aexit__ on exit, creating and closing the httpx client
        # within the correct event loop.
        async with self._velide_api as api_session:
            return await api_session.add_delivery(order)