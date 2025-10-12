# -*- coding: utf-8 -*-
"""
A PyQt5-based utility to check for new releases on a GitHub repository.

This module provides an `UpdateChecker` QObject that can be integrated into
any PyQt5 application to check for software updates in a non-blocking way.

Dependencies:
- PyQt5
- httpx

Install dependencies with:
pip install PyQt5 httpx
"""
import logging
import traceback
from typing import Tuple, Optional

import httpx
from PyQt5.QtCore import pyqtSignal, QRunnable, QObject, pyqtSlot

from models.config import InstallerConfig

class UpdateCheckerSignals(QObject):
    update_found = pyqtSignal(str, str)
    no_update_found = pyqtSignal()
    error = pyqtSignal(str, object) # (str, str | None)
    checking = pyqtSignal()


# --- Update Check Worker (for threading) ---
class UpdateCheckWorker(QRunnable):
    """
    Worker thread for checking GitHub releases without blocking the main GUI.

    Inherits from QRunnable to handle the execution in a separate thread.
    """

    def __init__(self, config: InstallerConfig):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._config = config
        self.signals = UpdateCheckerSignals()

    @staticmethod
    def _parse_version(version_str: str, logger: Optional[logging.Logger] = None) -> Optional[Tuple[int, ...]]:
        """
        Parses a semantic version string into a tuple of integers for comparison.
        Handles an optional 'v' prefix. e.g., 'v1.2.3' -> (1, 2, 3).
        Returns None if the format is invalid.
        """
        if version_str.startswith('v'):
            version_str = version_str[1:]
        try:
            parts = tuple(map(int, version_str.split('.')))
            return parts
        except (ValueError, TypeError):
            if logger is not None:
                logger.error(f"Formato de versão inválida encontrada: {version_str}")
            return None

    @pyqtSlot()
    def run(self):
        """
        The main logic for the worker thread. Fetches release data from GitHub API.
        """
        self.signals.checking.emit()
        owner = self._config.owner
        repo = self._config.repo
        current_version = self._config.current_version

        self.logger.info(
            f"Checking for updates for {owner}/{repo}..."
            f" Current version: {current_version}"
        )

        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        
        current_version_tuple = self._parse_version(current_version, self.logger)
        if not current_version_tuple:
            error_msg = f"Versão atual '{current_version}' está mal formatada."
            self.signals.error.emit(error_msg, None)
            return

        try:
            # Using a context manager for the httpx client ensures cleanup
            with httpx.Client(timeout=15.0) as client:
                headers = {"Accept": "application/vnd.github.v3+json"}
                response = client.get(api_url, headers=headers)
                response.raise_for_status()  # Raises HTTPStatusError for 4xx/5xx
            
            releases = response.json()

            if not releases:
                self.logger.info("Não há versões disponíveis no repositório.")
                self.signals.no_update_found.emit()
                return

            latest_release = releases[0]
            latest_version_str = latest_release.get('tag_name')
            
            if not latest_version_str:
                msg = "Última versão encontrada mas não possui 'tag_name'."
                self.signals.error.emit(msg, "Resposta da API está faltando 'tag_name'.")
                return

            self.logger.info(f"Última versão encontrada no GitHub: {latest_version_str}")
            
            latest_version_tuple = self._parse_version(latest_version_str, self.logger)
            if not latest_version_tuple:
                msg = f"Tag da última versão '{latest_version_str}' está mal formatada."
                self.signals.error.emit(msg, "Não foi possível formatar a tag do GitHub.")
                return

            if latest_version_tuple > current_version_tuple:
                self.logger.info(f"Nova versão encontrada: {latest_version_str}")
                zip_url = latest_release.get('zipball_url')
                if not zip_url:
                    msg = f"Nova versão {latest_version_str} não há 'zipball_url'."
                    self.signals.error.emit(msg, "Resposta da API está faltando 'zipball_url'.")
                else:
                    self.signals.update_found.emit(zip_url, latest_version_str)
            else:
                self.logger.info("Versão já está atualizada.")
                self.signals.no_update_found.emit()

        except httpx.RequestError as exc:
            msg = f"Erro de conexão: Falha ao conectar com o servidor."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                msg = f"Repositório não encontrado."
            elif code == 403:
                msg = "Tentativas excedidas. Por favor tente novamente depois."
            else:
                msg = f"Erro HTTP encontrado: {code}."
            self.logger.error(f"{msg} Resposta: {exc.response.text}", exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except ValueError:  # Catches json.JSONDecodeError
            msg = "Resposta inválida do servidor: Não foi possível decodificar o JSON."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except Exception:
            msg = "Um erro inesperado ocorreu."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())