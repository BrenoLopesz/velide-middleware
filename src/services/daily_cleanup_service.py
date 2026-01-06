import logging
from typing import Optional
from PyQt5.QtCore import QObject, QTimer, pyqtSlot

# Ensure this import matches your project structure
from services.sqlite_service import SQLiteService

class DailyCleanupService(QObject):
    """
    A service dedicated to scheduling and triggering the daily database cleanup.
    
    It allows the main application to start the routine once, and handles 
    the 24-hour cycle automatically.
    """

    def __init__(
        self, 
        sqlite_service: SQLiteService, 
        days_retention: int = 90, 
        parent: Optional[QObject] = None
    ):
        """
        Args:
            sqlite_service: Reference to the main database service.
            days_retention: How many days of data to keep (default 90).
            parent: Parent QObject.
        """
        super().__init__(parent)
        self.sqlite_service = sqlite_service
        self.days_retention = days_retention
        self.logger = logging.getLogger(__name__)

        # Setup the Timer
        self.timer = QTimer(self)
        self.timer.setSingleShot(False)
        
        # 24 hours in milliseconds: 24 * 60 * 60 * 1000
        self.day_in_ms = 24 * 60 * 60 * 1000 
        self.timer.setInterval(self.day_in_ms)
        self.timer.timeout.connect(self._trigger_cleanup)

        # Connect to the result signal to log outcome
        self.sqlite_service.prune_result.connect(self._on_cleanup_finished)

    def start_routine(self):
        """
        Starts the daily cleanup routine. 
        
        It runs the cleanup IMMEDIATELY upon calling, 
        and then schedules it to repeat every 24 hours.
        """
        if self.timer.isActive():
            self.logger.warning("Rotina de limpeza já está ativa.")
            return

        self.logger.debug("Iniciando rotina de limpeza diária.")
        
        # 1. Run immediately
        self._trigger_cleanup()
        
        # 2. Start the timer for subsequent runs
        self.timer.start()

    def stop_routine(self):
        """Stops the scheduled cleanup."""
        self.timer.stop()
        self.logger.info("Rotina de limpeza parada.")

    @pyqtSlot()
    def _trigger_cleanup(self):
        """Internal slot called by Timer or start_routine."""
        self.logger.debug("Disparando solicitação de limpeza agendada...")
        self.sqlite_service.request_prune_old_data(self.days_retention)

    @pyqtSlot(int)
    def _on_cleanup_finished(self, deleted_count: int):
        """Callback when the DB finishes the prune operation."""
        if deleted_count > 0:
            self.logger.info(
                f"Limpeza concluída com sucesso. {deleted_count} registros removidos."
            )
        else:
            self.logger.info(
                "Limpeza concluída. Nenhum registro antigo precisou ser removido."
            )