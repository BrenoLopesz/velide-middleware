import sys
import os
import subprocess
import logging
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable


class WorkerSignals(QObject):
    """
    Defines the signals available from a running worker thread.
    Supported signals are:
    - launched: Emitted when the process is successfully started.
    - error: str, passes an error message.
    """

    launched = pyqtSignal()
    error = pyqtSignal(str)


class BatchExecutorWorker(QRunnable):
    """
    Worker thread for running the batch file.
    Inherits from QRunnable to handle worker thread setup, signals, and wrap-up.
    """

    def __init__(self, bat_path):
        super().__init__()
        self.bat_path = bat_path
        self.signals = WorkerSignals()
        self.logger = logging.getLogger(__name__)

    def run(self):
        """
        Executes the batch script in a hidden window.
        """
        try:
            # Path and file extension validation.
            if not os.path.exists(self.bat_path):
                raise FileNotFoundError(
                    f"O arquivo não foi encontrado em: {self.bat_path}"
                )

            if not self.bat_path.lower().endswith(".bat"):
                raise ValueError("O arquivo fornecido não é um script .bat.")

            normalized_path = os.path.normpath(self.bat_path)

            self.logger.info(f"Iniciando a execução do arquivo batch: {self.bat_path}")

            # Flags for Windows:
            # - CREATE_NO_WINDOW: Do not create console window.
            # - DETACHED_PROCESS: The new process is executed independently.
            #   This is crucial for it to keep running after *this* closes.
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = (
                    subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                )

            subprocess.Popen(
                normalized_path,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                shell=True,
            )

            self.logger.info(
                "Script batch iniciado com sucesso. A aplicação principal pode fechar."
            )
            self.signals.launched.emit()

        except Exception as e:
            # Captura qualquer outra exceção e a emite como um sinal de erro.
            error_message = (
                f"Ocorreu um erro inesperado ao tentar executar o script: {e}"
            )
            self.logger.error(error_message)
            self.signals.error.emit(error_message)
