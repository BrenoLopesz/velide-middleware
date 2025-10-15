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
import sys
import traceback
from typing import Tuple, Optional

import httpx
from packaging.version import Version, parse, InvalidVersion
from PyQt5.QtCore import pyqtSignal, QRunnable, QObject, pyqtSlot

from models.config import InstallerConfig

class UpdateCheckerSignals(QObject):
    """
    Defines the signals available from the UpdateCheckWorker.
    """
    # Emitted when a new version with the required assets is found.
    # Provides: installer_url, signatures_url, new_version_string
    update_found = pyqtSignal(str, str, str)

    # Emitted when the current version is the latest.
    no_update_found = pyqtSignal()

    # Emitted when an error occurs.
    # Provides: user-friendly_message, technical_details
    error = pyqtSignal(str, object)

    # Emitted when the check process starts.
    checking = pyqtSignal()


# --- Update Check Worker (for threading) ---
class UpdateCheckWorker(QRunnable):
    """
    Worker thread for checking GitHub releases without blocking the main GUI.

    Inherits from QRunnable to handle the execution in a separate thread.
    """

    def __init__(self, config: InstallerConfig, current_version: Version):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._config = config
        self._current_version = current_version
        self.signals = UpdateCheckerSignals()
        
    def run(self):
        """
        The main logic for the worker thread. Fetches release data from GitHub API.
        """
        self.signals.checking.emit()
        owner = self._config.owner
        repo = self._config.repo
        current_version = self._current_version

        self.logger.info(
            f"Verificando atualizações em {owner}/{repo}..."
            f" Versão atual: {current_version}"
        )

        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        
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
            
            # Filter out pre-releases
            stable_releases = [r for r in releases if not r.get('prerelease', False)]

            if not stable_releases:
                self.logger.info("Não há versões estáveis disponíveis no repositório.")
                self.signals.no_update_found.emit()
                return

            latest_release = stable_releases[0]
            latest_version_str = latest_release.get('tag_name')
            
            if not latest_version_str:
                msg = "Última versão encontrada mas não possui 'tag_name'."
                self.signals.error.emit(msg, "Resposta da API está faltando 'tag_name'.")
                return

            self.logger.info(f"Última versão encontrada no GitHub: {latest_version_str}")
            
            
            latest_version = parse(latest_version_str)

            if latest_version > current_version:
                self.logger.info(f"Nova versão encontrada: {latest_version_str}")

                # Determine system architecture to find the correct installer
                is_64bit = sys.maxsize > 2**32
                installer_name = "velide_install_x64.exe" if is_64bit else "velide_install_x86.exe"
                self.logger.info(f"Procurando por assets: '{installer_name}' e 'signatures.json'")

                installer_url = None
                signatures_url = None

                assets = latest_release.get('assets', [])
                for asset in assets:
                    asset_name = asset.get('name')
                    if asset_name == installer_name:
                        installer_url = asset.get('browser_download_url')
                    elif asset_name == 'signatures.json':
                        signatures_url = asset.get('browser_download_url')

                if installer_url and signatures_url:
                    self.logger.info("Assets do instalador e de assinaturas encontrados.")
                    self.signals.update_found.emit(installer_url, signatures_url, latest_version_str)
                else:
                    msg = f"Nova versão {latest_version_str} não possui os assets necessários."
                    details = f"Instalador encontrado: {bool(installer_url)}. Assinatura encontrada: {bool(signatures_url)}."
                    self.logger.error(f"{msg} ({details})")
                    self.signals.error.emit(msg, details)
            else:
                self.logger.info("Versão já está atualizada.")
                self.signals.no_update_found.emit()

        except InvalidVersion:
            msg = "O conteúdo não corresponde a um formato de versão válido (SemVer)."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except httpx.RequestError as exc:
            msg = "Erro de conexão: Falha ao conectar com o servidor."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                msg = "Repositório não encontrado."
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