import logging
from typing import Any, Tuple
from datetime import datetime, time
from PyQt5.QtCore import QRunnable, QObject, pyqtSignal

from connectors.farmax.farmax_repository import FarmaxRepository

class FarmaxWorkerSignals(QObject):
    """
    Defines the signals available from a generic repository worker thread.
    
    Supported signals:
    
    finished
      Emitted when the task is completed, regardless of success.
    
    error
      str: Emitted when an error occurs. Passes a string 
      (str) with the error message.
    
    success
      object: Emitted when the task is successfully completed. 
      Passes the result (object) from the repository method.
    """
    finished = pyqtSignal()
    error = pyqtSignal(str)
    success = pyqtSignal(object)


class FarmaxWorker(QRunnable):
    """
    A QRunnable worker that uses creational class methods for
    type-safe execution of repository tasks in a separate thread.
    
    Do not instantiate this class directly. Instead, use one
    of the provided @classmethod factory methods.
    
    ---
    
    ### Example Usage:
    
    ```python
    # 1. To fetch deliverymen
    worker = FarmaxWorker.for_fetch_deliverymen(self.repository)
    worker.signals.success.connect(self.handle_deliverymen_list)
    
    # 2. To update a delivery
    worker = FarmaxWorker.for_update_delivery_as_done(
        self.repository, 
        delivery=my_delivery_obj, 
        ended_at=datetime.now().time()
    )
    worker.signals.success.connect(self.handle_update_success)
    
    # 3. To fetch specific deliveries
    ids_tuple = (123.0, 456.0)
    worker = FarmaxWorker.for_fetch_deliveries_by_id(
        self.repository, 
        cd_vendas=ids_tuple
    )
    worker.signals.success.connect(self.handle_deliveries_list)
    
    # Run any of them
    self.thread_pool.start(worker)
    ```
    """
    
    def __init__(self, repository: FarmaxRepository, method_name: str, *args: Any, **kwargs: Any):
        """
        Private constructor. Use the @classmethod factories to create
        a worker instance.
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.repository = repository
        self.signals = FarmaxWorkerSignals()
        
        # Store the task to be executed
        self.method_name = method_name
        self.args = args
        self.kwargs = kwargs

    def run(self):
        """
        The main execution method for the QRunnable.
        
        This method is called when the worker is run by a QThreadPool.
        It executes the repository method specified during construction.
        """
        try:
            # 1. Get the actual method from the repository object
            method_to_call = getattr(self.repository, self.method_name)
            
            # 2. Execute the task with the provided arguments
            result = method_to_call(*self.args, **self.kwargs)
            
            # 3. Emit the success signal with the result
            self.signals.success.emit(result)
            
        except Exception:
            error_msg = (
                f"Falha ao executar '{self.method_name}' no Farmax."
            )
            self.logger.exception(error_msg)
            self.signals.error.emit(error_msg)
            
        finally:
            # 4. Always emit finished
            self.signals.finished.emit()

    # --- Creational Factory Methods ---

    @classmethod
    def for_fetch_deliverymen(
        cls, 
        repository: FarmaxRepository
    ) -> 'FarmaxWorker':
        """Creates a worker to fetch all active deliverymen."""
        return cls(repository, "fetch_deliverymen")

    @classmethod
    def for_fetch_recent_changes(
        cls, 
        repository: FarmaxRepository, 
        last_check_time: datetime
    ) -> 'FarmaxWorker':
        """Creates a worker to fetch delivery log changes."""
        # We pass arguments as kwargs for clarity and safety
        return cls(
            repository, 
            "fetch_recent_changes", 
            last_check_time=last_check_time
        )
    
    @classmethod
    def for_fetch_recent_changes_by_id(
        cls, 
        repository: FarmaxRepository, 
        last_id: int
    ) -> 'FarmaxWorker':
        """Creates a worker to fetch delivery log changes, based on latest fetched ID."""
        # We pass arguments as kwargs for clarity and safety
        return cls(
            repository, 
            "fetch_recent_changes_by_id", 
            last_id=last_id
        )

    @classmethod
    def for_fetch_deliveries_by_id(
        cls, 
        repository: FarmaxRepository, 
        cd_vendas: Tuple[float, ...]
    ) -> 'FarmaxWorker':
        """Creates a worker to fetch full delivery details by ID."""
        return cls(
            repository, 
            "fetch_deliveries_by_id", 
            cd_vendas=cd_vendas
        )
        
    @classmethod
    def for_fetch_sales_statuses_by_id(
        cls, 
        repository: FarmaxRepository, 
        cd_vendas: Tuple[float, ...]
    ) -> 'FarmaxWorker':
        """Creates a worker to fetch sale statuses by ID."""
        return cls(
            repository, 
            "fetch_sales_statuses_by_id", 
            cd_vendas=cd_vendas
        )

    @classmethod
    def for_update_delivery_as_in_route(
        cls, 
        repository: FarmaxRepository, 
        sale_id: float, 
        driver_id: str, 
        left_at: time
    ) -> 'FarmaxWorker':
        """Creates a worker to update a delivery to 'In Route'."""
        return cls(
            repository, 
            "update_delivery_as_in_route", 
            sale_id=sale_id, 
            driver_id=driver_id, 
            left_at=left_at
        )

    @classmethod
    def for_update_delivery_as_done(
        cls, 
        repository: FarmaxRepository, 
        sale_id: float, 
        ended_at: time
    ) -> 'FarmaxWorker':
        """Creates a worker to update a delivery to 'Done'."""
        return cls(
            repository, 
            "update_delivery_as_done", 
            sale_id=sale_id, 
            ended_at=ended_at
        )