from PyQt5.QtCore import QObject, pyqtSignal
from datetime import datetime
import logging
from utils.log_messages import dashboard_message

LOG_LEVEL_MAP = {
    "CRITICAL": "Crítico",
    "ERROR": "Erro",
    "WARNING": "Aviso",
    "INFO": "Info",
    "DEBUG": "Debug",
    "NOTSET": "N/A",
}


class PackageFilter(logging.Filter):
    """
    Keep library warnings/errors visible, filtering only routine package chatter.
    """

    def __init__(self, excluded_packages):
        super().__init__()
        if isinstance(excluded_packages, str):
            self.excluded_packages = [excluded_packages]
        else:
            self.excluded_packages = excluded_packages

    def filter(self, record):
        """
        Match package boundaries so similarly named application loggers survive.
        """
        return record.levelno >= logging.WARNING or not any(
            record.name == name or record.name.startswith(name + ".") for name in self.excluded_packages)


class QLogHandler(logging.Handler, QObject):
    # Define a new signal that emits a list of strings
    new_log = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)

    def emit(self, record):
        """
        This method is called by the logging framework for each log record.
        """
        try:
            timestamp = datetime.fromtimestamp(record.created).strftime("%d/%m %H:%M:%S")
            level = LOG_LEVEL_MAP.get(record.levelname, record.levelname)
            self.new_log.emit(timestamp, level, dashboard_message(record))
        except Exception:
            self.handleError(record)
