# -*- coding: utf-8 -*-
"""
QRunnable worker to perform asynchronous downloads of the installer and its
signature file, with progress reports and error handling.
"""
import os
import time
import logging
import traceback
from typing import List, Tuple

import httpx
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot

class UpdateDownloaderSignals(QObject):
    """
    Defines the signals available for the download worker.
    """
    # Signal emitted with (bytes_downloaded, total_bytes) for the installer
    progress = pyqtSignal(int, int)

    # Signal emitted on successful completion of ALL downloads
    finished = pyqtSignal()

    # Signal emitted in case of error, sending (friendly_message, traceback_string)
    error = pyqtSignal(str, object)
    
    # Signal emitted when the download process starts
    started = pyqtSignal()

class UpdateDownloaderWorker(QRunnable):
    """
    Worker thread to download an installer and its signature file from URLs
    without blocking the GUI.

    Receives URLs and destination paths for both files, emits signals for
    progress, success, or failure.
    """
    # Throttle progress updates to a maximum of once every 100ms
    PROGRESS_THROTTLE_INTERVAL = 0.1  # seconds

    def __init__(self, files_to_download: List[Tuple[str, str, bool]]):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.signals = UpdateDownloaderSignals()

        self._files_to_download = files_to_download
        self.is_cancelled = False

    def _download_file(self, url: str, destination_path: str, report_progress: bool = False):
        """
        Downloads a single file from a URL to a destination path.

        This is a helper method that contains the core download logic.
        It can optionally report progress and will raise exceptions on failure,
        which are caught by the main `run` method.

        Args:
            url (str): The URL of the file to download.
            destination_path (str): The local path to save the file.
            report_progress (bool): If True, emit progress signals for this download.
        """
        self.logger.info(f"Baixando '{url}' para '{destination_path}'...")
        last_progress_time = 0

        with httpx.stream("GET", url, timeout=30.0, follow_redirects=True) as response:
            response.raise_for_status()

            total_size = int(response.headers.get("Content-Length", 0))
            downloaded_bytes = 0

            if report_progress:
                self.logger.info(f"Tamanho total do arquivo do instalador: {total_size} bytes.")

            # Create parent directories if they don't exist
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)

            with open(destination_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=8192):
                    if self.is_cancelled:
                        self.logger.warning(f"Download de '{url}' cancelado pelo usuário.")
                        return

                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    
                    if report_progress:
                        current_time = time.time()
                        if current_time - last_progress_time > self.PROGRESS_THROTTLE_INTERVAL:
                            self.signals.progress.emit(downloaded_bytes, total_size)
                            last_progress_time = current_time

        # After the loop, emit one final signal if needed to ensure the bar reaches 100%
        if report_progress and not self.is_cancelled:
            self.signals.progress.emit(downloaded_bytes, total_size)
        
        self.logger.info(f"Download de '{destination_path}' concluído com sucesso.")


    @pyqtSlot()
    def run(self):
        """
        Main worker logic. Executes the downloads for the installer and
        signatures file sequentially.
        """
        self.logger.info("Iniciando o processo de download da atualização...")
        self.signals.started.emit()

        try:
            for _file in self._files_to_download:
                # 1. Download the installer file (with progress reporting)
                self._download_file(
                    *_file
                )
                if self.is_cancelled:
                    return

            # 3. If both succeed, emit the finished signal
            self.logger.info("Download dos arquivos concluído com sucesso.")
            self.signals.finished.emit()

        except httpx.RequestError:
            msg = "Erro de conexão: Não foi possível alcançar o servidor de download."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            msg = f"Erro HTTP {code} ao tentar baixar o arquivo."
            self.logger.error(f"{msg} Resposta: {exc.response.text}", exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except (IOError, OSError):
            msg = "Erro de arquivo: Verifique as permissões ou o espaço em disco."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

        except Exception:
            msg = "Um erro inesperado ocorreu durante o download."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

    def cancel(self):
        """
        Signals the worker that the download should be cancelled.
        """
        self.logger.info("Sinal de cancelamento recebido.")
        self.is_cancelled = True
