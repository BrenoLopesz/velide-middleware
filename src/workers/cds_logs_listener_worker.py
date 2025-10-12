from PyQt5.QtCore import QObject, pyqtSignal
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
    def __init__(self, worker_instance):
        super().__init__()
        self.worker = worker_instance
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
            self.worker.new_order.emit(content)
        except json.JSONDecodeError as e:
            self.logger.exception(f"Falha ao decodificar JSON do arquivo: {filepath}")
        except (IOError, OSError, PermissionError) as e:
            self.logger.exception(f"Erro no sistema de arquivos ao ler arquivo: {filepath}")
        except Exception as e:
            self.logger.exception(f"Um erro inesperado ocorreu ao ler o arquivo: {filepath}")


class CdsLogsListenerWorker(QObject):
    """
    A QObject worker that monitors a folder for new JSON files matching a specific
    pattern ("ent*.json"). It uses the 'watchdog' library to detect file creation
    in a separate thread and emits signals with the file content or errors.
    """
    # Signal emitted when a new valid log file is found and parsed.
    # The payload will be the JSON content (dict or list).
    new_order = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, folder_path: str, parent=None):
        """
        Initializes the worker.
        
        Args:
            folder_path (str): The absolute path to the folder to monitor.
            parent (QObject, optional): The parent QObject. Defaults to None.
        """
        super().__init__(parent)
        self.folder_path = folder_path
        self._is_running = False
        self._observer = None
        self.logger = logging.getLogger(__name__)

        if not os.path.isdir(self.folder_path):
            msg = f"Diretório fornecido não é valido: {self.folder_path}"
            self.logger.error(msg)
            # Emit error immediately if path is invalid
            # Using a direct call to a slot or a timer might be necessary
            # if the event loop hasn't started yet. For simplicity, we raise an error.
            raise ValueError(msg)

    def run(self):
        """
        Starts the file system monitoring in a background thread.
        """
        if self._is_running:
            self.logger.warning("Observador já está sendo executado.")
            return

        try:
            event_handler = _NewFileHandler(self)
            self._observer = Observer()
            self._observer.schedule(event_handler, self.folder_path, recursive=False)
            self._observer.start()
            self._is_running = True
            self.logger.info(f"Monitoramento de entregas iniciado na pasta: {self.folder_path}")
        except Exception as e:
            self.logger.exception("Falha ao iniciar observervador de arquivos.")
            
    def stop(self):
        """
        Stops the file system monitoring.
        """
        if not self._is_running or not self._observer:
            self.logger.warning("Observador não está sendo executado.")
            self.finished.emit()
            return
            
        try:
            self._observer.stop()
            self._observer.join()  # Wait for the thread to terminate
            self._is_running = False
            self.logger.info(f"Monitoramento de arquivos foi parado: {self.folder_path}")
        except Exception as e:
            self.logger.exception("Um erro ocorreu ao tentar interromper o observador de arquivos.")
        finally:
            self.finished.emit()
