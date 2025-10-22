import logging
from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool, pyqtSlot

from workers.sqlite_worker import SQLiteWorker, SQLiteWorkerSignals

class SQLiteService(QObject):
    """
    A high-level service class (QObject) to manage SQLite operations
    asynchronously using the global QThreadPool.

    This class provides a clean API to the rest of the application.
    The main window will interact with this service, not directly
    with the workers.
    """
    
    # --- Service-Level Signals ---
    # These are the signals the main application will connect to.
    
    # Emits True/False on completion of an add operation
    add_mapping_result = pyqtSignal(bool)
    
    # Emits the found ID (str) or None
    local_id_found = pyqtSignal(object)
    
    # Emits the found ID (str) or None
    velide_id_found = pyqtSignal(object)
    
    # Emits True/False on completion of a delete operation
    delete_mapping_result = pyqtSignal(bool)
    
    # Emits a list of all mappings
    all_mappings_found = pyqtSignal(list)
    
    # Emits a simplified error message (str) if any worker fails
    error_occurred = pyqtSignal(str)
    

    def __init__(self, db_path: str, parent: Optional[QObject] = None):
        """
        Initializes the database service.
        
        Args:
            db_path (str): The path to the SQLite database file.
            parent (QObject, optional): The parent QObject.
        """
        super().__init__(parent)
        self.db_path = db_path
        self.thread_pool = QThreadPool.globalInstance()
        self.logger = logging.getLogger(__name__)
        self.logger.info(
            f"DatabaseService using global QThreadPool with "
            f"maxThreadCount={self.thread_pool.maxThreadCount()}"
        )

    def _create_and_run_worker(
        self, 
        factory_method, 
        *args, 
        result_signal: pyqtSignal
    ):
        """
        Internal helper method to create, connect, and run a worker.
        
        Args:
            factory_method: The SQLiteWorker classmethod (e.g., for_add_mapping).
            *args: Arguments for the factory method (e.g., velide_id).
            result_signal: The service-level signal to emit the result to.
        """
        # 1. Create the worker-specific signals
        worker_signals = SQLiteWorkerSignals()
        
        # 2. Create the worker using its factory
        worker = factory_method(worker_signals, self.db_path, *args)
        
        # 3. Connect the worker's signals to our service's signals
        #    - Connect the worker's 'result' to the specific service signal
        #    - Connect the worker's 'error' to the specific error signal
        worker.signals.result.connect(result_signal.emit)
        worker.signals.error.connect(self.error_occurred.emit)
        
        # 4. Start the worker in the global thread pool
        self.thread_pool.start(worker)

    @pyqtSlot(str, str)
    def request_add_mapping(self, velide_id: str, local_id: str):
        """Asynchronously adds a new mapping."""
        self.logger.debug(f"Solicitando para adicionar mapeamento: {velide_id} -> {local_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_add_mapping,
            velide_id,
            local_id,
            result_signal=self.add_mapping_result
        )

    @pyqtSlot(str)
    def request_get_local_id(self, velide_id: str):
        """Asynchronously retrieves a local_id."""
        self.logger.debug(f"Solicitando `local_id` para: {velide_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_get_local_id,
            velide_id,
            result_signal=self.local_id_found
        )

    @pyqtSlot(str)
    def request_get_velide_id(self, local_id: str):
        """Asynchronously retrieves a velide_id."""
        self.logger.debug(f"Solicitando `velide_id` para: {local_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_get_velide_id,
            local_id,
            result_signal=self.velide_id_found
        )

    @pyqtSlot(str)
    def request_delete_mapping(self, velide_id: str):
        """Asynchronously deletes a mapping."""
        self.logger.debug(f"Solicitando para deletar mapeamento de: {velide_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_delete_mapping,
            velide_id,
            result_signal=self.delete_mapping_result
        )

    @pyqtSlot()
    def request_get_all_mappings(self):
        """Asynchronously retrieves all mappings."""
        self.logger.debug("Solicitando todos mapeamentos.")
        self._create_and_run_worker(
            SQLiteWorker.for_get_all_mappings,
            result_signal=self.all_mappings_found
        )