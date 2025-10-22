import logging
from typing import List
from PyQt5.QtCore import QRunnable, QObject, pyqtSignal

from connectors.farmax.farmax_repository import FarmaxRepository
from models.farmax_models import FarmaxDeliveryman

class FarmaxWorkerSignals(QObject):
    """
    Defines the signals available from a worker thread.
    
    This class is used by QRunnable workers to communicate with the 
    main thread, as QRunnable itself cannot emit signals.

    Supported signals:
    
    finished
        Emitted when the task is completed, regardless of success.
    
    error
        str: Emitted when an error occurs. Passes a string 
        (str) with the error message.
    
    success
        list: Emitted when the data is successfully fetched. 
        Passes the list (list) of FarmaxDeliveryman objects.
    """
    finished = pyqtSignal()
    error = pyqtSignal(str)
    success = pyqtSignal(list) 


class FarmaxWorker(QRunnable):
    """
    A QRunnable worker to fetch deliverymen from the FarmaxRepository
    in a separate thread using a QThreadPool.
    
    This worker is designed to perform the database query without 
    blocking the main GUI thread. It uses a WorkerSignals object 
    to communicate the results or any errors back.
    """
    
    def __init__(self, repository: FarmaxRepository):
        """
        Initialize the worker.
        
        Args:
            repository (FarmaxRepository): An instance of the 
                                           repository to use for 
                                           database operations.
        """
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.repository = repository
        self.signals = FarmaxWorkerSignals()

    def run(self):
        """
        The main execution method for the QRunnable.
        
        This method is called when the worker is run by a QThreadPool.
        It attempts to fetch the list of deliverymen. On success, 
        it emits the 'success' signal with the data. On failure,
        it logs the error and emits the 'error' 
        signal. It always emits 'finished'
        when done.
        """
        try:
            # 1. Executa a tarefa principal (a consulta ao banco)
            deliverymen_list: List[FarmaxDeliveryman] = self.repository.fetch_deliverymen()
            
            # 2. Emite o sinal de sucesso com os dados
            self.signals.success.emit(deliverymen_list)
            
        except Exception as e:
            error_msg = f"Falha ao buscar a lista de entregadores no banco de dados: {e}"
            
            self.logger.exception(error_msg)
            
            self.signals.error.emit(error_msg)
            
        finally:
            self.signals.finished.emit()