"""Serial SQLite work outside the GUI thread, with post-commit callbacks."""
import logging
import sqlite3
from pathlib import Path
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
            method = self.method if callable(self.method) else getattr(self.journal, self.method)
            result = method(*self.args)
            self.signals.done.emit((self, result, None))
        except Exception as error:
            logging.getLogger(__name__).exception("Durable synchronization operation failed")
            self.signals.done.emit((self, None, str(error)))


class SyncJournalService(QObject):
    error = pyqtSignal(str)

    def __init__(self, path, scope, source_identity=None, source_timezone=None, ownership=None):
        super().__init__()
        self.journal = SyncJournal(path, scope, source_identity, source_timezone, ownership)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._callbacks = {}
        self._rollout_sealed = False
        self.failure_revision = 0

    def submit(self, method, *args, success=None, failure=None):
        if self._rollout_sealed:
            raise RuntimeError("Synchronization journal is sealed for rollout")
        work = _Work(self.journal, method, args)
        self._callbacks[work] = (success, failure)
        work.signals.done.connect(self._done)
        self.pool.start(work)

    @pyqtSlot(object)
    def _done(self, outcome):
        work, result, error = outcome
        success, failure = self._callbacks.pop(work)
        if error is not None:
            self.failure_revision += 1
            self.error.emit(error)
            if failure:
                failure(error)
        elif success:
            success(result)

    def pending_effects(self, success, failure):
        """Read ALL pending effects, including deferred/unowned/review work."""
        path = self.journal.path
        def read():
            conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
            try:
                return conn.execute("SELECT COUNT(*) FROM SyncEffects WHERE state='PENDING'").fetchone()[0]
            finally:
                conn.close()
        self.submit(read, success=success, failure=failure)
