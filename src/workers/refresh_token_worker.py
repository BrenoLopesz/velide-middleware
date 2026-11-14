from typing import cast
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
import requests
import json
import logging

from config import AuthenticationConfig
from models.exceptions import TokenStorageError
from utils.access_token import AccessTokenDict
from utils.token_storage import store_token_at_file


class RefreshTokenSignals(QObject):
    """
    Defines the signals available from the RefreshTokenRunnable.

    Inherits from QObject to provide signal/slot capabilities.
    """

    token = pyqtSignal(str, str)
    """Signal emitted on successful token refresh.
    
    Sends: (access_token, refresh_token)
    """

    finished = pyqtSignal()
    """Signal emitted when the task is complete, regardless of outcome."""

    error = pyqtSignal(str)
    """Signal emitted when there was an error while retrieving the token.
    
    Sends the error_message (str).
    """

    # Note: A signal for failure is not strictly needed here because
    # the 'finished' signal implies the end, and 'token' not being
    # emitted implies failure. The calling code can connect 'finished'
    # and then check if it received a new token.


class RefreshTokenWorker(QRunnable):
    """
    A QRunnable task to refresh an expired access token using a
    refresh token in a worker thread.

    Results are communicated via a separate 'signals' object.
    """

    def __init__(self, refresh_token: str, auth_config: AuthenticationConfig, commit_token=None, is_current=None):
        """
        Initializes the runnable worker.

        Args:
            refresh_token: The refresh token to use for the request.
            auth_config: The AuthenticationConfig object with domain,
                         client_id, etc.
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)

        # Store dependencies
        self._refresh_token = refresh_token
        self._commit_token = commit_token
        self._is_current = is_current or (lambda: True)
        self.signals = RefreshTokenSignals()

        # Prepare request details
        self.url = f"https://{auth_config.domain}/oauth/token"
        self.headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.data = {
            "client_id": auth_config.client_id,
            "grant_type": "refresh_token",
            "scope": auth_config.scope,
            "refresh_token": self._refresh_token,
        }

    def run(self):
        """
        The main execution method for the QRunnable.

        Attempts to fetch a new access token using the refresh token.
        On success, it emits the new token and stores the full response.
        On failure, it logs the error. It always emits 'finished'
        when the task is complete.
        """
        try:
            if not self._is_current():
                return
            response = requests.post(
                self.url, headers=self.headers, data=self.data, verify=True, timeout=15
            )

            # If the refresh token is revoked or the user was deleted, 
            # we need to fail explicitly so the app can logout.
            if response.status_code == 400:
                try:
                    error_data = response.json()
                    if error_data.get("error") == "invalid_grant":
                        self.signals.error.emit("Sessão expirada. Faça login novamente.")
                        return
                except ValueError:
                    pass  # Not JSON, proceed to standard error handling

            # Raise an exception for 4xx or 5xx status codes
            response.raise_for_status()

            jsonResponse = json.loads(response.text)

            # The response for a refresh token grant might not include a
            # new refresh token. We must re-add the one we just used
            # to ensure it's stored correctly for future use.
            jsonResponse["refresh_token"] = jsonResponse.get("refresh_token") or self._refresh_token
            jsonResponse = cast(AccessTokenDict, jsonResponse)

            access_token = jsonResponse["access_token"]

            # Store the new token bundle
            if self._commit_token is not None:
                if not self._commit_token(jsonResponse):
                    return
            else:
                store_token_at_file(jsonResponse)
            self.signals.token.emit(access_token, jsonResponse["refresh_token"])
        except requests.HTTPError:
            self.signals.error.emit("Servidor recusou a renovação do token.")
            # Catches non-2xx responses from raise_for_status()
            self.logger.exception(
                "Ocorreu um erro no servidor during a solitação " \
                "para recarregar token de acesso."
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            self.signals.error.emit("Falha de conexão ao renovar token.")
            # Catches network-level errors
            self.logger.exception(
                "Falha ao conectar com o servidor para recarregar token de acesso."
            )
        except requests.RequestException:
            self.signals.error.emit("Falha ao renovar token.")
            # A catch-all for other 'requests' library errors
            self.logger.exception(
                "Falha ao solicitar recarregamento do token de acesso."
            )
        except json.JSONDecodeError:
            self.signals.error.emit("Resposta de autenticação inválida.")
            self.logger.exception("Servidor não retornou um JSON válido.")
        except TokenStorageError:
            self.signals.error.emit("Falha ao armazenar token renovado.")
            self.logger.exception("Falha ao armazenar token de acesso.")
        except Exception:
            self.signals.error.emit("Erro inesperado ao renovar token.")
            self.logger.exception("Erro inesperado ao recarregar o token de acesso.")
        finally:
            # Always emit the 'finished' signal
            self.signals.finished.emit()
