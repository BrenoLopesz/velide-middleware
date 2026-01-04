import logging
import asyncio
from typing import Dict, Optional

from PyQt5.QtCore import QRunnable, QObject, pyqtSignal
from gql import gql, Client
from gql.transport.websockets import WebsocketsTransport
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError

from config import ApiConfig
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

    # 1. ADDED: This signal was missing
    finished = pyqtSignal()

    # 2. ADDED: Essential for the Service Adapter
    status_changed = pyqtSignal(bool)


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

        # Control flag
        self._is_running = True

        # Reference to transport to allow forcing close if needed
        self._transport: Optional[WebsocketsTransport] = None

    def stop(self):
        """Method to call from the main thread to stop the worker."""
        self._is_running = False
        self.logger.info("Solicitação de parada do WebSocket recebida.")
        # 3. IMPROVEMENT: Force close transport to break the await immediately
        if self._transport:
            # We schedule the close, as we are likely in a different thread
            try:
                loop = asyncio.get_event_loop()
                # Check if loop is running to avoid RuntimeError
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._transport.close(), loop)
            except Exception:
                pass

    def run(self) -> None:
        try:
            self.logger.info("Iniciando thread do WebSocket Velide...")
            asyncio.run(self._run_async())
        except Exception as e:
            self.logger.exception("Falha fatal no Worker.")
            self.signals.error_occurred.emit(str(e))
        finally:
            self.signals.connection_closed.emit("Worker finished.")
            # Ensure we tell the service we are offline effectively
            self.signals.status_changed.emit(False)

            # 3. ADDED: Emit 'finished' so the Service knows the thread is dead
            self.signals.finished.emit()

    async def _run_async(self) -> None:
        # Defines the subscription query
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

        variables: Dict[str, str] = {"authorization": self.access_token}

        retry_delay = 2

        while self._is_running:
            try:
                # Protocol: graphql-transport-ws
                # We use init_payload={} because the server 
                # expects the standard handshake
                self._transport = WebsocketsTransport(
                    url=self.api_config.velide_websockets_server,
                    init_payload={},
                    keep_alive_timeout=60,
                    ping_interval=30,
                )

                async with Client(
                    transport=self._transport, fetch_schema_from_transport=False
                ) as session:
                    self.logger.info("Conectado ao WebSocket Velide.")

                    # Signal: Connected (Green Light)
                    self.signals.status_changed.emit(True)

                    retry_delay = 2  # Reset delay

                    async for data in session.subscribe(
                        subscription_query, variable_values=variables
                    ):
                        if not self._is_running:
                            break

                        try:
                            # Use .get to be safe
                            action_data = data.get("latestAction")
                            if action_data:
                                validated_action = LatestAction.model_validate(
                                    action_data
                                )
                                self.signals.action_received.emit(validated_action)

                        except ValidationError as e:
                            self.logger.error(f"Erro de validação: {e}")
                            # Do not emit error signal here to avoid spamming the UI
                            # if the server sends bad data repeatedly
                        except KeyError:
                            self.logger.error("Dados incompletos recebidos.")

            except (
                ConnectionClosedError,
                ConnectionRefusedError,
                asyncio.TimeoutError,
                OSError,
            ) as e:
                # Signal: Disconnected (Red Light)
                self.signals.status_changed.emit(False)

                self.logger.warning(
                    f"Conexão perdida ({e}). Tentando reconectar em {retry_delay}s..."
                )
                # Note: We do NOT emit error_occurred here to avoid spamming the UI
                # with popups. The status_changed(False) 
                # is enough for the UI to turn red.

            except Exception as e:
                self.logger.exception("Erro inesperado no loop Websocket.")
                self.signals.status_changed.emit(False)
                self.signals.error_occurred.emit(str(e))

            finally:
                self._transport = None

            if self._is_running:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
