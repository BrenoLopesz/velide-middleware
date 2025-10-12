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
    A filter that allows log records only from a specific package and its children.
    """
    def __init__(self, package_name):
        super().__init__()
        self.package_name = package_name

    def filter(self, record):
        # The `logging` module uses dot-notation for logger names.
        # record.name for a logger from "src.views.main" will be "src.views.main".
        # We check if the record's name starts with our desired package name.
        return record.name.startswith(self.package_name)

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