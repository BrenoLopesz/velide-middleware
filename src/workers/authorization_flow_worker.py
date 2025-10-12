from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from utils.device_code import DeviceCode
from utils.access_token import AccessToken
# The worker no longer needs to import these directly
# from utils.enviroment_variables import DOMAIN, CLIENT_ID, SCOPE, AUDIENCE 
import logging
import time
import traceback
from typing import Dict
from config import AuthenticationConfig
from models.exceptions import NetworkError, ApiError, TokenPollingError
from utils.token_storage import store_token_at_file

class AuthorizationFlowWorker(QObject):
    """
    Handles the device authorization flow in a background thread.
    """
    # --- Error Constants ---
    ERROR_NETWORK = "Não foi possível conectar ao servidor.<br/>Por favor, verifique sua conexão com a internet."
    ERROR_CODE_EXPIRED = "Código do dispositivo expirado."
    ERROR_UNEXPECTED = "Ocorreu um erro inesperado."

    # --- Signals ---
    authenticated = pyqtSignal(str)
    # Emits (user_message, detailed_traceback)
    error = pyqtSignal(str, str)
    device_code = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, config: AuthenticationConfig) -> None:
        super().__init__()
        self.logger = logging.getLogger(__name__)
        # Dependency Injection: Configuration is now passed in
        self.config = config
        self._is_running = True # Flag to potentially allow for cancellation

    def _emit_error(self, message: str, exception: Exception = None):
        """Helper to format and emit the error signal."""
        stacktrace = str(exception) + "\n" + traceback.format_exc() if exception else "No exception info."
        self.logger.error(f"{message} - Traceback: {stacktrace}")
        self.error.emit(message, stacktrace)

    def _get_device_code(self) -> Dict:
        """Requests the initial device code and verification URI."""
        device_code_handler = DeviceCode(
            domain=self.config.domain,
            client_id=self.config.client_id,
            scope=self.config.scope,
            audience=self.config.audience
        )
        return device_code_handler.request()

    def _poll_for_access_token(self, device_code_info: Dict) -> str:
        """Polls the token endpoint until an access token is granted or time expires."""
        expires_at = time.time() + device_code_info["expires_in"]
        interval = device_code_info["interval"]
        
        access_token_handler = AccessToken(
            domain=self.config.domain,
            client_id=self.config.client_id,
            device_code=device_code_info["device_code"]
        )

        while time.time() < expires_at:
            try:
                token_data = access_token_handler.request()
                # SUCCESS! We got the token.
                store_token_at_file(token_data) # Call the robust storage function
                return token_data['access_token']

            except TokenPollingError as e:
                # Handle specific OAuth errors
                if e.error_code == 'authorization_pending':
                    # This is normal, just continue polling
                    self.logger.debug("Autorização pendente, continuando soliticações...")
                elif e.error_code == 'slow_down':
                    # Server is asking us to poll less frequently
                    interval += 5 # Increase the interval
                    self.logger.warning("Servidor solicitou para ir devagar.")
                elif e.error_code in ['access_denied', 'expired_token']:
                    # These are terminal errors, stop polling.
                    self.logger.error(f"Erro terminal de autenticação: {e.error_code}")
                    # Re-raise or emit a specific error signal from here
                    raise e 
                else:
                    # Unknown OAuth error
                    raise e # Let the main 'run' method handle it

            except (NetworkError, ApiError) as e:
                # Handle connection or unexpected server errors
                self.logger.error(f"Parando solicitações devido à erro na rede/API: {e}")
                raise e # Let the main 'run' method catch and report this

            time.sleep(interval)

        # If the loop finishes, the code expired
        return None

    @pyqtSlot()
    def run(self):
        """Main execution method for the worker."""
        try:
            # Step 1: Get device code
            device_code_info = self._get_device_code()
            self.device_code.emit(device_code_info)
            
            # Step 2: Poll for the access token
            access_token = self._poll_for_access_token(device_code_info)
            
            # Step 3: Handle the result
            if access_token:
                self.authenticated.emit(access_token)
            else:
                # This handles both expired and cancelled scenarios
                if self._is_running:
                    self._emit_error(self.ERROR_CODE_EXPIRED)

        except NetworkError as e:
            # Specific handling for network issues
            self._emit_error(self.ERROR_NETWORK, e)

        except ApiError as e:
            # You can craft a more detailed message for the user
            user_message = (f"O servidor retornou um erro ({e.status_code}).<br/>"
                            f"Por favor, tente novamente mais tarde.")
            self._emit_error(user_message, e)

        except Exception as e:
            # A catch-all for any other unexpected errors
            self._emit_error(self.ERROR_UNEXPECTED, e)
        finally:
            self.finished.emit()
            
    def stop(self):
        """Public method to signal the worker to stop polling."""
        self._is_running = False