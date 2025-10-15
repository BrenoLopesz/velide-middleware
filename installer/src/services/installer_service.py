# -*- coding: utf-8 -*-

"""
This module provides a service class that manages the execution of the
InstallerRunnable. It abstracts the thread management away from the UI.
"""

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from workers.installer_runnable_worker import InstallerRunnableWorker

class InstallerService(QObject):
    """
    Service to manage running the Inno Setup installer in the background.

    This class provides a clean API for the main application to use. It
    handles the creation of the runnable and its execution in the global
    QThreadPool.
    """
    # Define signals that the service will emit to the UI
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.thread_pool = QThreadPool.globalInstance()

    def start_installation(self, installer_path: str):
        """
        Creates and starts an InstallerRunnable for the given installer path.

        Args:
            installer_path (str): The path to the installer executable.
        """
        # Create a new worker for this specific installation task
        worker = InstallerRunnableWorker(installer_path)

        # Connect the worker's signals to the service's relay methods
        worker.signals.finished.connect(self._on_worker_finished)
        worker.signals.error.connect(self._on_worker_error)

        # Start the worker in the thread pool
        self.thread_pool.start(worker)

    def _on_worker_finished(self):
        """
        Relays the 'finished' signal from the worker to the UI.
        """
        self.finished.emit()

    def _on_worker_error(self, message: str):
        """
        Relays the 'error' signal from the worker to the UI.
        """
        self.error.emit(message)
