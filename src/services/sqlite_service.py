import logging
from typing import Any, List, Optional, Tuple

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

    # Emits the number of rows successfully inserted (int)
    add_many_mappings_result = pyqtSignal(int)
    
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

    # --- Delivery Service-Level Signals ---
    
    # Emits True/False on completion of adding a delivery
    add_delivery_result = pyqtSignal(bool)

    # Emits the number of rows successfully inserted (int)
    add_many_deliveries_result = pyqtSignal(int)

    # Emits True/False on completion of status update
    update_status_result = pyqtSignal(bool)

    # Emits a tuple (internal_id, DeliveryStatus) or None
    delivery_by_external_found = pyqtSignal(object)

    # Emits a tuple (external_id, DeliveryStatus) or None
    delivery_by_internal_found = pyqtSignal(object)

    # Emits a list of tuples [(ext_id, int_id, status), ...]
    all_deliveries_found = pyqtSignal(list)
    

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

    def _create_and_run_worker(
        self, 
        factory_method, 
        *args, 
        result_signal: Any
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

    @pyqtSlot(list)
    def request_add_many_mappings(self, mappings: List[Tuple[str, str]]):
        """Asynchronously adds multiple new mappings, ignoring duplicates."""
        self.logger.debug(f"Solicitando {len(mappings)} mapeamentos para serem adicionados.")
        self._create_and_run_worker(
            SQLiteWorker.for_add_many_mappings,
            mappings,  # Note: 'mappings' is a single list argument, matching *args
            result_signal=self.add_many_mappings_result
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

    # -----------------------------------------------------------------
    # DeliveryMapping Requests
    # -----------------------------------------------------------------

    @pyqtSlot(str, str, object) # 'object' allows passing the Enum
    def request_add_delivery_mapping(self, external_id: str, internal_id: str, status):
        """Asynchronously adds a new delivery mapping."""
        self.logger.debug(f"Solicitando adicionar entrega: {external_id} -> {internal_id} ({status})")
        self._create_and_run_worker(
            SQLiteWorker.for_add_delivery_mapping,
            external_id,
            internal_id,
            status,
            result_signal=self.add_delivery_result
        )

    @pyqtSlot(list)
    def request_add_many_delivery_mappings(self, mappings: List[Tuple[str, str, object]]):
        """
        Asynchronously adds multiple delivery mappings.
        Expected format: [(ext_id, int_id, DeliveryStatus), ...]
        """
        self.logger.debug(f"Solicitando adicionar {len(mappings)} mapeamentos de entrega.")
        self._create_and_run_worker(
            SQLiteWorker.for_add_many_delivery_mappings,
            mappings,
            result_signal=self.add_many_deliveries_result
        )

    @pyqtSlot(str, object, str) # str, Enum, str (Optional is technically just `object` or `str` in signals)
    def request_update_delivery_status(self, external_id: str, new_status, deliveryman_id: Optional[str] = None):
        """
        Asynchronously updates the status of a delivery.
        """
        self.logger.debug(f"Solicitando atualização: {external_id} -> {new_status} (Entregador: {deliveryman_id})")
        self._create_and_run_worker(
            SQLiteWorker.for_update_delivery_status,
            external_id,
            new_status,
            deliveryman_id,
            result_signal=self.update_status_result
        )

    @pyqtSlot(str)
    def request_get_delivery_by_external(self, external_id: str):
        """Asynchronously retrieves delivery info by external ID."""
        self.logger.debug(f"Solicitando busca de entrega por external ID: {external_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_get_delivery_by_external,
            external_id,
            result_signal=self.delivery_by_external_found
        )

    @pyqtSlot(str)
    def request_get_delivery_by_internal(self, internal_id: str):
        """Asynchronously retrieves delivery info by internal ID."""
        self.logger.debug(f"Solicitando busca de entrega por internal ID: {internal_id}")
        self._create_and_run_worker(
            SQLiteWorker.for_get_delivery_by_internal,
            internal_id,
            result_signal=self.delivery_by_internal_found
        )

    @pyqtSlot()
    def request_get_all_deliveries(self):
        """Asynchronously retrieves all delivery mappings."""
        self.logger.debug("Solicitando todas as entregas.")
        self._create_and_run_worker(
            SQLiteWorker.for_get_all_deliveries,
            result_signal=self.all_deliveries_found
        )

    @pyqtSlot()
    def request_get_active_deliveries(self):
        """Asynchronously retrieves all delivery mappings."""
        self.logger.debug("Solicitando todas as entregas ativas.")
        self._create_and_run_worker(
            SQLiteWorker.for_get_active_deliveries,
            result_signal=self.all_deliveries_found
        )