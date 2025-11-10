import logging
import asyncio
from typing import Dict, Any

from PyQt5.QtCore import QRunnable, QObject, pyqtSignal
from gql import gql, Client
from gql.transport.websockets import WebsocketsTransport
from pydantic import ValidationError

# --- Assuming these are in your project ---
from config import ApiConfig
# We'll import the Pydantic model from the file below
from models.velide_websockets_models import LatestAction 

class VelideWebsocketsSignals(QObject):
    """
    Defines signals emitted by the VelideWebsocketsWorker.
    
    Signals:
        action_received (object): Emitted when a new, validated action is 
                                  received. The payload is the 
                                  `LatestAction` Pydantic model instance.
        error_occurred (str): Emitted when any error occurs (connection, 
                              validation, etc.). The payload is the 
                              error message.
        connection_closed (str): Emitted when the websocket connection is 
                                 closed, either intentionally or 
                                 unexpectedly.
    """
    action_received = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    connection_closed = pyqtSignal(str)


class VelideWebsocketsWorker(QRunnable):
    """
    A QRunnable worker that connects to the Velide GraphQL WebSocket server,
    listens for subscriptions, validates incoming data, and emits signals.
    """
    
    def __init__(self, api_config: ApiConfig, access_token: str):
        """
        Initializes the worker.
        
        Args:
            api_config: An object containing configuration, including
                        `velide_websockets_server` and `auth_token`.
                        
        Raises:
            ValueError: If required configuration is missing.
        """
        super().__init__()
        
        if not api_config.velide_websockets_server:
            raise ValueError("Velide Websockets server URL is not defined.")
            
        if not access_token:
            raise ValueError("Velide auth_token is not defined.")

        self.api_config = api_config
        self.access_token = access_token
        self.signals = VelideWebsocketsSignals()
        self.logger = logging.getLogger(__name__)


        # 1. Configure Transport with standard auth headers
        # This payload is sent once during the 'connection_init' message
        auth_payload = {
            "headers": {
                "Authorization": f"Bearer {self.api_config.auth_token}"
            }
        }
        
        self.transport = WebsocketsTransport(
            url=self.api_config.velide_websockets_server,
            connect_params=auth_payload
        )

        # 2. Configure GQL Client
        self.client = Client(
            transport=self.transport,
            fetch_schema_from_transport=True
        )

    def run(self) -> None:
        """
        Synchronous entry point for the QRunnable.
        This method sets up and runs the asynchronous event loop.
        """
        try:
            self.logger.info("Inciando conexão com o Velide...")
            # asyncio.run() creates a new event loop and runs the coroutine
            asyncio.run(self._run_async())
        
        except Exception as e:
            # Catch any unexpected shutdown errors
            self.logger.exception(f"Ocorreu uma falha inesperada durante a conexão com o Velide.")
            self.signals.error_occurred.emit(str(e))
        
        finally:
            self.logger.info("Conexão com o Velide foi fechada.")
            self.signals.connection_closed.emit("Conexão fechada.")

    async def _run_async(self):
        """
        The main asynchronous logic for the WebSocket subscription.
        """
        
        # 3. Define the GraphQL Subscription Query
        subscription_query = gql("""
            subscription LatestAction($authorization: String!) {
                latestAction(authorization: $authorization) {
                    actionType
                    timestamp
                    offset
                    deliveryman {
                        id
                        name
                    }
                    delivery {
                        id
                        code
                    }
                }
            }
        """)

        # 4. Define the variables for the subscription
        # This is passed with the 'subscribe' message
        # Note: This is separate from the 'connect_params' auth
        variables: Dict[str, Any] = {
            "authorization": self.api_config.auth_token
        }
        
        try:
            # 5. Start the client session and subscribe
            async with self.client as session:
                # The 'subscribe' method returns an async generator
                async for data in session.subscribe(
                    subscription_query,
                    variable_values=variables
                ):
                    self.logger.debug(f"Raw data received: {data}")
                    
                    # 6. Validate the received data with Pydantic
                    try:
                        # Extract the data nested under the query name
                        action_data = data['latestAction']
                        
                        # Validate and parse the data
                        # This raises ValidationError if parsing fails
                        validated_action = LatestAction.model_validate(action_data)
                        
                        self.logger.debug(f"Received valid action: {validated_action.action_type}")
                        
                        # 7. Emit the validated Pydantic model
                        self.signals.action_received.emit(validated_action)

                    except ValidationError as e:
                        self.logger.warning(f"Falha ao validar dados recebidos do Velide.")
                        self.signals.error_occurred.emit(f"Erro de validação: {e}")
                    except KeyError:
                        self.logger.warning(f"Recebido dado faltando informações, durante conexão com Velide.")
                        self.signals.error_occurred.emit("Estrutura dos dados inválida")

        except Exception as e:
            # Handles connection errors, transport errors, etc.
            self.logger.exception(f"Ocorreu um erro inesperado.")
            self.signals.error_occurred.emit(f"Erro no WebSocket: {e}")