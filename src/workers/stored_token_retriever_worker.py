from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
import jwt
from models.exceptions import TokenStorageError
from utils.token_storage import read_token_from_file
import logging


def auth_context(config):
    return {name: getattr(config, name) for name in ("domain", "client_id", "audience", "scope")}


def stored_session_matches(bundle, config):
    """Cache eligibility only. The API still verifies token signatures/ownership."""
    if bundle.get("middleware_auth") is not None:
        return bundle["middleware_auth"] == auth_context(config)
    try:
        token = bundle["access_token"]
        if token.startswith("Bearer "):
            token = token[7:]
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
        issuer = config.domain if "://" in config.domain else "https://" + config.domain
        audiences = claims.get("aud", [])
        if isinstance(audiences, str):
            audiences = [audiences]
        scope = bundle.get("scope") or claims.get("scope")
        if isinstance(scope, str):
            requested = set(config.scope.split())
            granted = set(scope.split())
            if bundle.get("refresh_token"):
                granted.add("offline_access")
            if not requested.issubset(granted):
                return False
        return (claims.get("iss", "").rstrip("/") == issuer.rstrip("/")
                and config.audience in audiences and claims.get("azp") == config.client_id)
    except (jwt.InvalidTokenError, KeyError, TypeError, AttributeError):
        return False


class StoredTokenRetrieverSignals(QObject):
    """
    Defines the signals available from the StoredTokenRetrieverRunnable.

    Inherits from QObject to provide signal/slot capabilities.
    """

    finished = pyqtSignal()
    """Signal emitted when the task is complete, regardless of outcome."""

    expired = pyqtSignal(str)
    """Signal emitted if the stored access token is expired.
    
    Sends the refresh_token (str) so a new one can be requested.
    """

    token = pyqtSignal(str, str)
    """Signal emitted when a valid, non-expired access token is found.
    
    Sends: (access_token, refresh_token)
    """

    error = pyqtSignal(str)
    """Signal emitted when there was an error while retrieving the token.
    
    Sends the error_message (str).
    """

class StoredTokenRetrieverWorker(QRunnable):
    """
    A QRunnable task to retrieve a stored token from a file in a worker thread.

    It checks if the token exists and if it has expired.
    Results are communicated via a separate 'signals' object.
    """

    def __init__(self, auth_config=None):
        """
        Initializes the runnable worker.
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.signals = StoredTokenRetrieverSignals()
        self.auth_config = auth_config

    def _is_token_expired(self, access_token: str) -> bool:
        """
        Checks if a JWT access token is expired without verifying its signature.

        Args:
            access_token: The JWT access token string.

        Returns:
            True if the token is expired or invalid, False otherwise.
        """
        try:
            jwt_options = {"verify_signature": False, "verify_exp": True}
            # This will raise ExpiredSignatureError (a subclass of InvalidTokenError)
            # if the 'exp' claim is in the past.
            _jwt = jwt.decode(access_token, options=jwt_options)
            return False
        except jwt.InvalidTokenError:
            # Catches ExpiredSignatureError, InvalidTokenError, etc.
            self.logger.warning(
                "Código de acesso expirado ou inválido. " \
                "Um novo token será solicitado..."
            )
            return True

    def run(self):
        """
        The main execution method for the QRunnable.

        Reads the token, checks its validity, and emits signals via
        the self.signals object.
        """
        try:
            token = read_token_from_file()

            if token is None:
                self.signals.error.emit("Nenhum token encontrado.")
                # No token found, just finish
                return

            access_token = token["access_token"]
            refresh_token = token["refresh_token"]

            if self.auth_config is not None and not stored_session_matches(token, self.auth_config):
                self.signals.error.emit("A conexão foi atualizada. Entre novamente para continuar usando o Velide.")
                return

            if self._is_token_expired(access_token):
                # Token is expired, emit signal with refresh token
                self.signals.expired.emit(refresh_token)
                return

            # Token is valid, emit signal with access token
            self.signals.token.emit(access_token, refresh_token)

        except TokenStorageError as e:
            self.signals.error.emit(f"Falha ao atualizar token: {str(e)}")
            self.logger.exception("Falha ao obter token armazenado.")
        except Exception as e:
            self.signals.error.emit(f"Falha ao atualizar token: {str(e)}")
            self.logger.exception(
                "Ocorreu um erro inesperado ao buscar o token armazenado."
            )
            self.signals.error.emit(str(e))
        finally:
            # Always emit finished signal when done
            self.signals.finished.emit()
