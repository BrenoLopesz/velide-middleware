from PyQt5.QtCore import QObject, pyqtSignal
from datetime import datetime
import logging

LOG_LEVEL_MAP = {
    'CRITICAL': "Crítico",
    'ERROR': "Erro",
    'WARNING': "Aviso",
    'INFO': "Info",
    'DEBUG': "Debug",
    'NOTSET': "N/A",
}

class PackageFilter(logging.Filter):
    """
    A filter that blocks log records from specific third-party packages.
    """
    def __init__(self, excluded_packages):
        super().__init__()
        if isinstance(excluded_packages, str):
            self.excluded_packages = [excluded_packages]
        else:
            self.excluded_packages = excluded_packages

    def filter(self, record):
        """
        Blocks a log record if its name starts with any excluded package.
        """
        return not any(record.name.startswith(name) for name in self.excluded_packages)

class QLogHandler(logging.Handler, QObject):
    # Define a new signal that emits a list of strings
    new_log = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__()
        QObject.__init__(self, parent)

    def emit(self, record):
        """
        This method is called by the logging framework for each log record.
        """
        # We manually format the message components to a list
        timestamp = datetime.fromtimestamp(record.created).strftime("%m/%d %H:%M:%S")
        level = LOG_LEVEL_MAP.get(record.levelname, record.levelname)
        message = record.message # Get the formatted message
        
        # Emit the signal with the log data
        self.new_log.emit(timestamp, level, message)