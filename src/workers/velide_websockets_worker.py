import logging
import asyncio
from typing import Dict, Optional

from PyQt5.QtCore import QRunnable, QObject, pyqtSignal
from gql import gql, Client
from gql.transport.websockets import WebsocketsTransport
from gql.transport.exceptions import TransportError
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError

# Custom imports
from config import ApiConfig
from models.velide_websockets_models import LatestAction
from services.auth_service import AuthService
from utils.async_token_provider import AsyncTokenProvider
from utils.connection_state import ConnectionState


class VelideWebsocketsSignals(QObject):
    """
    Defines signals emitted by the VelideWebsocketsWorker.

    Signals:
        action_received (object): Emitted when a new, validated action is
            received. The payload is the `LatestAction` Pydantic model instance.
        error_occurred (str): Emitted when a non-recoverable or significant
            error occurs. The payload is the error message.
        connection_closed (str): Emitted when the websocket connection is
            closed, either intentionally or unexpectedly.
        connection_state_changed (ConnectionState): Emitted when the
            connection status changes (CONNECTING, CONNECTED, DISCONNECTED, ERROR).
        finished (None): Emitted when the worker thread completely shuts down.
    """
    action_received = pyqtSignal(object)
    error_occurred = pyqtSignal(str)
    connection_closed = pyqtSignal(str)
    connection_state_changed = pyqtSignal(ConnectionState)
    finished = pyqtSignal()


class VelideWebsocketsWorker(QRunnable):
    """
    A QRunnable worker that connects to the Velide GraphQL WebSocket server.
    
    Features:
    - Auto-reconnection with exponential backoff.
    - Token refreshing via AsyncTokenProvider.
    - Robust error handling for network and protocol issues.
    - Pydantic model validation for incoming data.
    """

    def __init__(self, api_config: ApiConfig, auth_service: AuthService):
        """
        Initializes the worker.

        Args:
            api_config: Configuration object containing the Websocket URL.
            auth_service: The service responsible for token management, passed
                          to the AsyncTokenProvider.

        Raises:
            ValueError: If the Websockets server URL is missing.
        """
        super().__init__()

        if not api_config.velide_websockets_server:
            raise ValueError("Velide Websockets server URL is not defined.")

        self.api_config = api_config
        self.auth_service = auth_service
        self.signals = VelideWebsocketsSignals()
        self.logger = logging.getLogger(__name__)

        # Control flags
        self._is_running = True
        self._transport: Optional[WebsocketsTransport] = None
        
        # Retry logic state
        self._retry_delay = 2

        # Pre-compile the query for performance
        self._subscription_query = gql("""
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
                        routeId
                        createdAt
                        endedAt
                    }
                    route {
                        id
                        deliveries {
                            id
                            createdAt
                        }
                    }
                }
            }
        """)

    def stop(self):
        """
        Thread-safe method to stop the worker.
        Signals the loop to exit and force-closes the active transport.
        """
        self._is_running = False
        self.logger.info("Solicitação de parada do WebSocket recebida.")
        
        if self._transport:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._transport.close(), loop)
            except Exception:
                # Ignore errors during shutdown sequence
                pass

    def run(self):
        """
        Entry point for the QRunnable. Sets up the asyncio loop.
        """
        try:
            self.logger.debug("Iniciando thread do WebSocket Velide...")
            asyncio.run(self._run_main_loop())
        except Exception as e:
            self.logger.exception("Falha fatal no WebSocket.")
            self.signals.error_occurred.emit(str(e))
        finally:
            self.signals.connection_closed.emit("Worker encerrado.")
            self.signals.finished.emit()

    async def _run_main_loop(self):
        """
        The main lifecycle loop. Handles token retrieval, connection attempts,
        and exponential backoff logic.
        """
        self._retry_delay = 2

        while self._is_running:
            try:
                # 1. State: Connecting
                self.signals.connection_state_changed.emit(ConnectionState.CONNECTING)

                # 2. Self-Healing: Get a fresh token before every connection attempt
                token = await AsyncTokenProvider.get_valid_token(self.auth_service)

                # 3. Connect and Listen
                await self._subscribe_and_listen(token)

            except (ConnectionClosedError, OSError, asyncio.TimeoutError) as e:
                self.logger.warning(
                    f"Conexão perdida ou falha na rede ({type(e).__name__}: {e}). "
                    f"Tentando reconectar em {self._retry_delay}s..."
                )
                self.signals.connection_state_changed.emit(
                    ConnectionState.DISCONNECTED
                )

            except TransportError as e:
                # Should restart almost immediately. This is cleaned up later.
                self._retry_delay = 0.5
                self.logger.warning(
                    f"Erro de transporte (possível rejeição de token): {e}"
                )
                self.signals.connection_state_changed.emit(
                    ConnectionState.DISCONNECTED
                )
                # We don't break here; the next loop will fetch a fresh token.

            except Exception as e:
                # Critical logic error or unexpected crash
                error_msg = str(e)
                self.logger.exception("Erro inesperado no WebSocket.")
                
                # Check for fatal auth errors to stop spamming
                if "Sessão expirada" in error_msg:
                    self.signals.error_occurred.emit(
                        "Sessão expirada. Parando conexões."
                    )
                    self.signals.connection_state_changed.emit(ConnectionState.ERROR)
                    break

                self.signals.connection_state_changed.emit(ConnectionState.ERROR)
                self.signals.error_occurred.emit(f"Erro interno: {error_msg}")

            # Wait before retrying (Exponential Backoff)
            if self._is_running:
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, 60)
        
        # Loop finished cleanly
        self.signals.connection_state_changed.emit(ConnectionState.DISCONNECTED)

    async def _subscribe_and_listen(self, token: str):
        """
        Establishes the WebSocket connection and processes the subscription stream.
        
        Args:
            token: The valid authorization token.
        """
        # Protocol: graphql-transport-ws
        self._transport = WebsocketsTransport(
            url=self.api_config.velide_websockets_server,
            init_payload={},  # Standard handshake
            keep_alive_timeout=60,
            ping_interval=30
        )

        variables: Dict[str, str] = {"authorization": token}

        async with Client(
            transport=self._transport, fetch_schema_from_transport=False
        ) as session:
            self.logger.info("Conectado ao Velide.")
            self.signals.connection_state_changed.emit(ConnectionState.CONNECTED)
            
            # CRITICAL: Reset retry delay upon successful connection
            self._retry_delay = 2

            async for data in session.subscribe(
                self._subscription_query, 
                variable_values=variables
            ):
                if not self._is_running:
                    break
                
                # Safe data extraction
                action_data = data.get("latestAction")
                if action_data:
                    try:
                        validated_action = LatestAction.model_validate(action_data)
                        self.signals.action_received.emit(validated_action)
                    except ValidationError:
                        self.logger.exception("Erro de validação de dados.")
                        # We do not disconnect, just log and skip the bad frame
                    except KeyError:
                        self.logger.exception("Dados incompletos recebidos no payload.")