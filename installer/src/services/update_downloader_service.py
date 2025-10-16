# -*- coding: utf-8 -*-
"""
A service class to manage the file download process in a non-blocking way,
following the MVPS (Model-View-Presenter-Service) pattern.
"""
import logging
from typing import List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QThreadPool

from workers.update_downloader_worker import UpdateDownloaderWorker

class UpdateDownloaderService(QObject):
    """
    Manages the lifecycle of the UpdateDownloaderWorker.

    This service is responsible for creating, connecting signals, and
    running the download worker in a background thread pool. It exposes
    clean signals for the Presenter layer to consume.
    """
    # --- Public signals for the Presenter ---
    # Emitted with (bytes_downloaded, total_bytes) for the installer download
    progress = pyqtSignal(int, int)

    # Emitted on success with the paths to both downloaded files
    # Provides: installer_path, signatures_path
    finished = pyqtSignal()

    # Emitted on error with a user-friendly message and technical details
    error = pyqtSignal(str, object)

    # Emitted when the download process begins
    download_started = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        """
        Initializes the UpdateDownloaderService.

        Args:
            parent (QObject, optional): The parent QObject. Defaults to None.
        """
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.threadpool = QThreadPool.globalInstance()
        self.worker: Optional[UpdateDownloaderWorker] = None

    @pyqtSlot(list)
    def start_download(self, files_to_download: List[Tuple[str, str, bool]]):
        """
        Starts the file downloads in a background thread.

        Instantiates the UpdateDownloaderWorker, connects its signals,
        and starts it in the global thread pool.

        Args:
            installer_url (str): The URL of the installer to download.
            installer_path (str): The local file path for the installer.
            signatures_url (str): The URL of the signatures file to download.
            signatures_path (str): The local file path for the signatures file.
        """
        if self.worker is not None:
            self.logger.warning("Um download já está em andamento. A nova solicitação foi ignorada.")
            return

        self.logger.info(
            f"Serviço de download inicializado."
        )

        self.worker = UpdateDownloaderWorker(
            files_to_download
        )

        # Connect the worker's internal signals to this service's public signals
        self.worker.signals.progress.connect(self.progress)
        self.worker.signals.finished.connect(self.finished)
        self.worker.signals.error.connect(self.error)
        self.worker.signals.started.connect(self.download_started)

        # When the worker is done, clear the reference to allow for new downloads
        self.worker.signals.finished.connect(self._on_task_finished)
        self.worker.signals.error.connect(self._on_task_finished)

        # Execute the worker
        self.threadpool.start(self.worker)

    def cancel_download(self):
        """
        Requests cancellation of the ongoing download.
        """
        if self.worker:
            self.logger.info("Solicitando o cancelamento do download...")
            self.worker.cancel()
        else:
            self.logger.warning("Nenhum download em andamento para cancelar.")


    def _on_task_finished(self):
        """
        Internal slot to clean up the worker reference after it has finished.
        """
        self.logger.info("Tarefa de download concluída. Limpando a referência do worker.")
        self.worker = None
