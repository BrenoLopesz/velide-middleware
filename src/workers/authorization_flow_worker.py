from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
from utils.device_code import DeviceCode, DeviceCodeDict
from utils.access_token import AccessToken, AccessTokenDict
import logging
import time
import traceback
from typing import Dict, Optional
from config import AuthenticationConfig
from models.exceptions import NetworkError, ApiError, TokenPollingError
from utils.token_storage import store_token_at_file


class AuthorizationFlowSignals(QObject):
    """
    Defines the signals available from the AuthorizationFlowRunnable.

    Inherits from QObject to provide signal/slot capabilities for the
    QRunnable worker.
    """

    authenticated = pyqtSignal(str, str)
    """Signal emitted upon successful authentication.
    
    Sends the access_token and the refresh_token (str, str).
    """

    error = pyqtSignal(str, str)
    """Signal emitted when an error occurs during the flow.
    
    Sends a user-friendly message (str) and a detailed traceback (str).
    """

    device_code = pyqtSignal(dict)
    """Signal emitted after successfully retrieving the device code and
    verification URI.
    
    Sends the device code info dictionary (dict).
    """

    finished = pyqtSignal()
    """Signal emitted when the entire authorization flow is complete,
    regardless of whether it succeeded or failed.
    """


class AuthorizationFlowWorker(QRunnable):
    """
    A QRunnable task to handle the OAuth 2.0 Device Authorization Flow
    in a worker thread.

    It requests a device code, emits it so the user can see it,
    and then polls for the access token. Results are communicated
    via a separate 'signals' object.
    """

    # --- Error Constants ---
    ERROR_NETWORK = (
        "Não foi possível conectar ao servidor.<br/>" \
        "Por favor, verifique sua conexão com a internet."
    )
    ERROR_CODE_EXPIRED = "Código do dispositivo expirado."
    ERROR_UNEXPECTED = "Ocorreu um erro inesperado."
    MISSING_REFRESH_TOKEN = "Erro: Token de atualização não recebido."

    def __init__(self, config: AuthenticationConfig):
        """
        Initializes the runnable worker.

        Args:
            config: An AuthenticationConfig object with endpoint details
                    (domain, client_id, etc.).
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.config = config
        self.signals = AuthorizationFlowSignals()
        self._is_running = True  # Flag to allow for cancellation

    def _emit_error(self, message: str, exception: Optional[Exception] = None):
        """
        Helper method to format and emit the error signal.

        Args:
            message: The user-friendly error message.
            exception: The exception object (if any) for logging.
        """
        stacktrace = (
            str(exception) + "\n" + traceback.format_exc()
            if exception
            else "No exception info."
        )
        self.logger.error(f"{message} - Traceback: {stacktrace}")
        self.signals.error.emit(message, stacktrace)

    def _get_device_code(self) -> DeviceCodeDict:
        """
        Requests the initial device code and verification URI from the
        authorization server.

        Raises:
            NetworkError: If a connection error occurs.
            ApiError: If the server returns an unexpected error.

        Returns:
            A dictionary containing device_code, verification_uri, etc.
        """
        device_code_handler = DeviceCode(
            domain=self.config.domain,
            client_id=self.config.client_id,
            scope=self.config.scope,
            audience=self.config.audience,
        )
        return device_code_handler.request()

    def _poll_for_access_token(
            self, 
            device_code_info: Dict
    ) -> Optional[AccessTokenDict]:
        """
        Polls the token endpoint until an access token is granted, the
        code expires, or the task is cancelled.

        Args:
            device_code_info: The dictionary received from _get_device_code.

        Returns:
            The token data on success, or None if the loop
            is exited (due to expiry or cancellation).
        """
        expires_at = time.time() + device_code_info["expires_in"]
        interval = device_code_info["interval"]

        access_token_handler = AccessToken(
            domain=self.config.domain,
            client_id=self.config.client_id,
            device_code=device_code_info["device_code"],
        )

        # Loop while NOT cancelled AND token not expired
        while self._is_running and time.time() < expires_at:
            try:
                token_data = access_token_handler.request()
                # SUCCESS! We got the token.
                store_token_at_file(token_data)  # Store it securely
                return token_data

            except TokenPollingError as e:
                # Handle specific OAuth errors
                if e.error_code == "authorization_pending":
                    # This is normal, just continue polling
                    self.logger.debug(
                        "Autorização pendente, continuando soliticações..."
                    )
                elif e.error_code == "slow_down":
                    # Server is asking us to poll less frequently
                    interval += 5  # Increase the interval
                    self.logger.warning("Servidor solicitou para ir devagar.")
                elif e.error_code in ["access_denied", "expired_token"]:
                    # These are terminal errors, stop polling.
                    self.logger.error(f"Erro terminal de autenticação: {e.error_code}")
                    raise e  # Re-raise to be caught by the main run() method
                else:
                    # Unknown OAuth error
                    raise e  # Let the main 'run' method handle it

            except (NetworkError, ApiError) as e:
                # Handle connection or unexpected server errors
                self.logger.error(
                    f"Parando solicitações devido à erro na rede/API: {e}"
                )
                raise e  # Let the main 'run' method catch and report this

            # Wait for the specified interval, but check for cancellation
            # to avoid a long, unnecessary sleep if stop() was called.
            # We do this by sleeping in small chunks.
            sleep_end = time.time() + interval
            while self._is_running and time.time() < sleep_end:
                time.sleep(0.5)  # Sleep in 500ms increments

        # If the loop finishes, the code either expired or was cancelled
        if not self._is_running:
            self.logger.debug("Polling foi cancelado pelo usuário.")

        return None

    def run(self):
        """
        The main execution method for the QRunnable.

        Performs the full device authorization flow:
        1. Gets the device code.
        2. Emits the code via the signals object.
        3. Polls for the access token.
        4. Emits the token on success or an error on failure.
        5. Emits 'finished' when done.
        """
        try:
            # Step 1: Get device code
            device_code_info = self._get_device_code()
            if not self._is_running:  # Check for cancellation after first network call
                return
            self.signals.device_code.emit(device_code_info)

            # Step 2: Poll for the access token
            token_data = self._poll_for_access_token(device_code_info)

            # Step 3: Handle the result
            if token_data:
                access_token = token_data.get("access_token")
                refresh_token = token_data.get("refresh_token")

                if access_token and refresh_token:
                    # Emit both tokens!
                    self.signals.authenticated.emit(access_token, refresh_token)
                else:
                    # Edge case: Server didn't send a refresh token? 
                    # Check your Auth0/Cognito "Offline Access" scope settings.
                    self._emit_error(self.MISSING_REFRESH_TOKEN)
            else:
                # This handles both expired and cancelled scenarios
                if self._is_running:
                    # Only emit "expired" error if we weren't manually stopped
                    self._emit_error(self.ERROR_CODE_EXPIRED)
                # If 'self._is_running' is false, we just finish silently

        except NetworkError as e:
            # Specific handling for network issues
            self._emit_error(self.ERROR_NETWORK, e)

        except ApiError as e:
            # Craft a more detailed message for the user
            user_message = (
                f"O servidor retornou um erro ({e.status_code}).<br/>"
                f"Por favor, tente novamente mais tarde."
            )
            self._emit_error(user_message, e)

        except Exception as e:
            # A catch-all for any other unexpected errors
            self._emit_error(self.ERROR_UNEXPECTED, e)
        finally:
            # Always emit 'finished'
            self.signals.finished.emit()

    def stop(self):
        """
        Public method to signal the worker to stop polling.
        The worker will stop polling and exit cleanly.
        """
        self.logger.debug("Sinal de parada recebido...")
        self._is_running = False
