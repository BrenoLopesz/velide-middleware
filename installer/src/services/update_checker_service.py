from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QThreadPool

from models.config import InstallerConfig
from workers.updates_checker_worker import UpdateCheckWorker

import logging

class UpdateCheckerService(QObject):
    """
    A QObject that checks for new releases on GitHub in a non-blocking way.

    This is the main class to be instantiated in your application.

    Args:
        owner (str): The owner of the GitHub repository.
        repo (str): The name of the GitHub repository.
        current_version (str): The current version of the application (e.g., 'v1.2.3').
        parent (QObject, optional): The parent QObject. Defaults to None.
    """
    # Public signals that users of this class can connect to
    update_found = pyqtSignal(str, str)
    no_update_found = pyqtSignal()
    error = pyqtSignal(str, object)
    checking_for_update = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.threadpool = QThreadPool.globalInstance()
        
    @pyqtSlot()
    def check_for_update(self, config: InstallerConfig):
        """
        Starts the update check in a background thread.
        """
        self.logger.info(
            f"UpdateChecker inicializado para {config.owner}/{config.repo}. "
            f"Max threads: {self.threadpool.maxThreadCount()}"
        )

        worker = UpdateCheckWorker(config)
        
        # Connect worker signals to this class's public signals
        worker.signals.update_found.connect(self.update_found)
        worker.signals.no_update_found.connect(self.no_update_found)
        worker.signals.error.connect(self.error)
        worker.signals.checking.connect(self.checking_for_update)
        
        # Start the worker in the thread pool
        self.threadpool.start(worker)
