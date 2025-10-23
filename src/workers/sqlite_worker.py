import sys
import logging
from typing import List, Tuple

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal

from api.sqlite_manager import SQLiteManager

class SQLiteWorkerSignals(QObject):
    """
    Defines the signals available from a running SQLiteWorker.
    
    QRunnable itself cannot emit signals, so we use a separate QObject
    to handle signaling.
    """
    
    # Signal emitted when the task is finished.
    finished = pyqtSignal()
    
    # Signal emitted with exception info (type, value, traceback) if an error occurs.
    error = pyqtSignal(tuple)
    
    # Signal emitted with the result of the operation.
    # We use 'object' as the type to handle any return type 
    # (bool, str, None, list, etc.).
    result = pyqtSignal(object)


class SQLiteWorker(QRunnable):
    """
    QRunnable worker for executing DeliverymenMappingDB operations in a thread pool.
    
    This class uses classmethod factories to create instances configured
    for specific database operations.
    """

    def __init__(self, signals: SQLiteWorkerSignals, db_path: str, operation_name: str, *args):
        """
        Generic initializer. It's recommended to use the @classmethod
        factories instead of calling this directly.
        
        Args:
            signals: The signal object to emit results/errors.
            db_path: Path to the SQLite database.
            operation_name: The string name of the method to call on DeliverymenMappingDB.
            *args: Arguments to pass to the method.
        """
        super().__init__()
        self.signals = signals
        self.db_path = db_path
        self.operation_name = operation_name
        self.args = args
        self.logger = logging.getLogger(__name__)

    # --- Factory Methods (Creational Pattern) ---

    @classmethod
    def for_add_mapping(
        cls, signals: SQLiteWorkerSignals, db_path: str, velide_id: str, local_id: str
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'add_mapping'."""
        return cls(signals, db_path, 'add_mapping', velide_id, local_id)
    
    @classmethod
    def for_add_many_mappings(
        cls, signals: SQLiteWorkerSignals, db_path: str, mappings: List[Tuple[str, str]]
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'add_many_mappings'."""
        return cls(signals, db_path, 'add_many_mappings', mappings)

    @classmethod
    def for_get_local_id(
        cls, signals: SQLiteWorkerSignals, db_path: str, velide_id: str
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'get_local_id'."""
        return cls(signals, db_path, 'get_local_id', velide_id)

    @classmethod
    def for_get_velide_id(
        cls, signals: SQLiteWorkerSignals, db_path: str, local_id: str
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'get_velide_id'."""
        return cls(signals, db_path, 'get_velide_id', local_id)

    @classmethod
    def for_delete_mapping(
        cls, signals: SQLiteWorkerSignals, db_path: str, velide_id: str
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'delete_mapping_by_velide_id'."""
        return cls(signals, db_path, 'delete_mapping_by_velide_id', velide_id)

    @classmethod
    def for_get_all_mappings(
        cls, signals: SQLiteWorkerSignals, db_path: str
    ) -> 'SQLiteWorker':
        """Factory method to create a worker for 'get_all_mappings'."""
        return cls(signals, db_path, 'get_all_mappings')

    # --- QRunnable Execution ---

    def run(self):
        """
        The main work method. This is executed in a separate thread
        by the QThreadPool.
        """
        try:
            # We use the SQLiteManager as a context manager,
            # just as it was designed.
            with SQLiteManager(self.db_path) as db:
                
                # Get the actual method to call from the db instance
                # using its string name (e.g., 'add_mapping')
                method_to_call = getattr(db, self.operation_name)
                
                # Call the method with the stored arguments
                op_result = method_to_call(*self.args)
                
                # Emit the result back to the main thread
                self.signals.result.emit(op_result)
                
        except Exception as e:
            # An error occurred, emit the error signal with traceback info
            self.logger.exception(f"Erro no SQLiteWorker executando {self.operation_name}")
            tb_info = sys.exc_info()
            self.signals.error.emit(tb_info)
            
        finally:
            # Always emit the 'finished' signal
            self.signals.finished.emit()