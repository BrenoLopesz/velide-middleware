
import logging
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from workers.batch_executor_worker import BatchExecutorWorker

class BatchExecutorService(QObject):
    """
    Manages the execution of a .bat file as a detached process.
    """
    # Emitted when the script is LAUNCHED succesfully.
    launched = pyqtSignal()
    # Signal emitted when failed to TRY to run the script.
    error = pyqtSignal(str)

    def __init__(self, bat_path: str):
        super().__init__()
        if not isinstance(bat_path, str) or not bat_path:
            raise ValueError("O caminho para o arquivo .bat deve ser uma string não vazia.")

        self.bat_path = bat_path
        self.threadpool = QThreadPool()
        logging.debug(f"BatchExecutorWorker inicializado para o arquivo: {self.bat_path}")

    def execute(self):
        """
        Starts the execution of the batch file in a separate thread.
        """
        runner = BatchExecutorWorker(self.bat_path)
        runner.signals.launched.connect(self.launched.emit)
        runner.signals.error.connect(self.error.emit)
        self.threadpool.start(runner)
