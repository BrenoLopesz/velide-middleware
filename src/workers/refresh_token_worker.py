from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
import requests
import json
import logging

from config import AuthenticationConfig
from models.exceptions import TokenStorageError
from utils.token_storage import store_token_at_file

class RefreshTokenWorker(QObject):
    token = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, refresh_token, auth_config: AuthenticationConfig):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._refresh_token = refresh_token
        self.url = f'https://{auth_config.domain}/oauth/token'
        self.headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        self.data = {
            'client_id': auth_config.client_id,
            'grant_type': "refresh_token",
            'scope': auth_config.scope,
            'refresh_token': refresh_token
        }

    @pyqtSlot()
    def run(self):
        try:
            response = requests.post(self.url, headers=self.headers, data=self.data, verify=False)

            response.raise_for_status()
            
            jsonResponse = json.loads(response.text)
            jsonResponse["refresh_token"] = self._refresh_token
            access_token = jsonResponse["access_token"]
            self.token.emit(access_token)


            store_token_at_file(jsonResponse)
        except requests.HTTPError:
            # This catches non-2xx responses from raise_for_status()
            self.logger.exception("Ocorreu um erro no servidor durante a solitação para recarregar token de acesso.")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            # This catches non-2xx responses from raise_for_status()
            self.logger.exception("Falha ao conectar com o servidor para recarregar token de acesso.")
        except requests.RequestException:
            self.logger.exception("Falha ao solicitar recarregamento do token de acesso.")
        except json.JSONDecodeError:
            self.logger.exception("Servidor não retornou um JSON válido.")
        except TokenStorageError:
            self.logger.exception("Falha ao armazenar token de acesso.")
        except Exception:
            self.logger.exception("Erro inesperado ao recarregar o token de acesso.")
        finally:
            self.finished.emit()