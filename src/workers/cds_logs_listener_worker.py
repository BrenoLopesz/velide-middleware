from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import logging
import json
import os
import time


class _NewFileHandler(FileSystemEventHandler):
    """
    An internal event handler for watchdog that processes file creation events.
    It's designed to be used exclusively by CdsLogsListenerWorker.
    """
    def __init__(self, signals_instance: 'CdsLogsListenerSignals'):
        """
        Initializes the handler.
        
        Args:
            signals_instance (CdsLogsListenerSignals): The QObject responsible
                                                        for emitting signals.
        """
        super().__init__()
        self.signals = signals_instance  # Store the signals object
        self.logger = logging.getLogger(__name__)

    def on_created(self, event):
        """
        Called when a file or directory is created.
        """
        if event.is_directory:
            return  # Ignore directory creation events

        try:
            filepath = event.src_path
            filename = os.path.basename(filepath)

            # Check if the file matches the specified naming convention
            if filename.startswith("ent") and filename.endswith(".json"):
                self.logger.debug(f"Novo potencial pedido encontrado: {filename}")
                self._process_file(filepath)
        except Exception as e:
            # Catch any unexpected errors during event handling
            self.logger.exception("Um erro inesperado ocorreu no gerenciador de arquivos adicionados.")
            
    def _process_file(self, filepath):
        """
        Reads and parses the JSON file, emitting signals on success or failure.
        """
        try:
            # A small delay can help prevent reading files that are still being written.
            time.sleep(0.1) 
            
            with open(filepath, 'r', encoding='windows-1252') as f:
                content = json.load(f)
            
            self.logger.debug(f"Conteúdo JSON lido com sucesso de: {filepath}")
            # Emit the signal via the signals object
            self.signals.new_order.emit(content)
        except json.JSONDecodeError as e:
            self.logger.exception(f"Falha ao decodificar JSON do arquivo: {filepath}")
        except (IOError, OSError, PermissionError) as e:
            self.logger.exception(f"Erro no sistema de arquivos ao ler arquivo: {filepath}")
        except Exception as e:
            self.logger.exception(f"Um erro inesperado ocorreu ao ler o arquivo: {filepath}")

class CdsLogsListenerSignals(QObject):
    """
    A separate QObject class to hold signals for the CdsLogsListenerWorker.
    This is necessary because QRunnable does not inherit from QObject and cannot
    define or emit its own pyqtSignals.
    """
    
    # Signal emitted when a new valid log file is found and parsed.
    # The payload will be the JSON content (dict or list).
    new_order = pyqtSignal(object)
    
    # Signal emitted when the worker runnable has finished its execution.
    finished = pyqtSignal()

    # Signal emitted when an error occurs (e.g., invalid path, observer crash).
    error = pyqtSignal(str)

class CdsLogsListenerWorker(QRunnable):
    """
    A QRunnable worker that monitors a folder for new JSON files matching a specific
    pattern ("ent*.json"). It uses the 'watchdog' library to detect file creation
    and emits signals via a separate CdsLogsListenerSignals object.
    
    To use this, create an instance and submit it to a QThreadPool.
    """

    def __init__(self, folder_path: str):
        """
        Initializes the worker.
        
        Args:
            folder_path (str): The absolute path to the folder to monitor.
        """
        super().__init__()
        self.folder_path = folder_path
        self._observer = None
        self.logger = logging.getLogger(__name__)
        
        # Create the separate signals object
        self.signals = CdsLogsListenerSignals()

    def run(self):
        """
        The main execution method for the QRunnable.
        Starts the file system monitoring and blocks until stop() is called.
        """
        try:
            if not os.path.isdir(self.folder_path):
                msg = f"Diretório fornecido não é valido: {self.folder_path}"
                self.logger.error(msg)
                self.signals.error.emit(msg)  # Emit error signal
                return  # Stop execution

            event_handler = _NewFileHandler(self.signals)  # Pass the signals object
            self._observer = Observer()
            self._observer.schedule(event_handler, self.folder_path, recursive=False)
            self._observer.start()
            self.logger.info(f"Monitoramento de entregas iniciado na pasta: {self.folder_path}")
        except Exception as e:
            self.logger.exception("Falha ao iniciar ou executar o observador de arquivos.")
            self.signals.error.emit(f"Erro no observador: {e}")
        finally:
            if self._observer:
                self._observer.join()  # Wait for the observer thread to terminate
            self.logger.info(f"Monitoramento de arquivos foi parado: {self.folder_path}")
            self.signals.finished.emit()  # Signal that this runnable is done

    def stop(self):
        """
        Stops the file system monitoring.
        """
        if not self._observer:
            self.logger.warning("Observador não está sendo executado.")
            self.finished.emit()
            return
            
        try:
            self._observer.stop()
            self._observer.join()  # Wait for the thread to terminate
            self.logger.info(f"Monitoramento de arquivos foi parado: {self.folder_path}")
        except Exception as e:
            self.logger.exception("Um erro ocorreu ao tentar interromper o observador de arquivos.")
        finally:
            self.finished.emit()
