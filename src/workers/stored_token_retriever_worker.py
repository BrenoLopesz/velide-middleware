from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
import jwt
from models.exceptions import TokenStorageError
from utils.token_storage import read_token_from_file
import logging


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

    token = pyqtSignal(str)
    """Signal emitted when a valid, non-expired access token is found.
    
    Sends the access_token (str).
    """


class StoredTokenRetrieverWorker(QRunnable):
    """
    A QRunnable task to retrieve a stored token from a file in a worker thread.

    It checks if the token exists and if it has expired.
    Results are communicated via a separate 'signals' object.
    """

    def __init__(self):
        """
        Initializes the runnable worker.
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.signals = StoredTokenRetrieverSignals()

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
                # No token found, just finish
                return

            access_token = token["access_token"]
            refresh_token = token["refresh_token"]

            if self._is_token_expired(access_token):
                # Token is expired, emit signal with refresh token
                self.signals.expired.emit(refresh_token)
                return

            # Token is valid, emit signal with access token
            self.signals.token.emit(token["access_token"])

        except TokenStorageError:
            self.logger.exception("Falha ao obter token armazenado.")
        except Exception:
            self.logger.exception("Ocorreu um erro inesperado.")
        finally:
            # Always emit finished signal when done
            self.signals.finished.emit()
