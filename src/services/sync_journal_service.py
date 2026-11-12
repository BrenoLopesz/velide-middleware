"""Serial SQLite work outside the GUI thread, with post-commit callbacks."""
import logging
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot

from api.sync_journal import SyncJournal


class _Signals(QObject):
    done = pyqtSignal(object)


class _Work(QRunnable):
    def __init__(self, journal, method, args):
        super().__init__()
        self.signals = _Signals()
        self.journal, self.method, self.args = journal, method, args

    def run(self):
        try:
            result = getattr(self.journal, self.method)(*self.args)
            self.signals.done.emit((self, result, None))
        except Exception as error:
            logging.getLogger(__name__).exception("Durable synchronization operation failed")
            self.signals.done.emit((self, None, str(error)))


class SyncJournalService(QObject):
    error = pyqtSignal(str)

    def __init__(self, path, scope, source_identity=None, source_timezone=None):
        super().__init__()
        self.journal = SyncJournal(path, scope, source_identity, source_timezone)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._callbacks = {}

    def submit(self, method, *args, success=None, failure=None):
        work = _Work(self.journal, method, args)
        self._callbacks[work] = (success, failure)
        work.signals.done.connect(self._done)
        self.pool.start(work)

    @pyqtSlot(object)
    def _done(self, outcome):
        work, result, error = outcome
        success, failure = self._callbacks.pop(work)
        if error is not None:
            self.error.emit(error)
            if failure:
                failure(error)
        elif success:
            success(result)
