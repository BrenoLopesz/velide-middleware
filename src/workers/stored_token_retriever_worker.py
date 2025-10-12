from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
import jwt
from models.exceptions import TokenStorageError
from utils.token_storage import read_token_from_file
import logging

class StoredTokenRetrieverWorker(QObject):
    finished = pyqtSignal()
    expired = pyqtSignal(str)
    token = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)

    def _is_token_expired(self, access_token: str):
        try:
            jwt_options = { 
                "verify_signature": False,
                "verify_exp": True
            }
            _jwt = jwt.decode(access_token, options=jwt_options)
            return False
        except jwt.InvalidTokenError:
            self.logger.warning("Código de acesso expirado ou inválido. Um novo token será solicitado...")
            return True

    @pyqtSlot()
    def run(self):
        try:
            token = read_token_from_file()

            if token is None:
                return
            
            access_token = token["access_token"]
            refresh_token = token["refresh_token"]

            if self._is_token_expired(access_token):
                self.expired.emit(refresh_token)
                return

            self.token.emit(token['access_token'])
        except TokenStorageError:
            self.logger.exception("Falha ao obter token armazenado.")
        except Exception:
            self.logger.exception("Ocorreu um erro inesperado.")
        finally:
            self.finished.emit()
