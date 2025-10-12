# decorators.py (Correct, reusable version)
from functools import wraps
from PyQt5.QtCore import QObject, QThread
import logging

logger = logging.getLogger(__name__)

def run_in_thread(prefix: str):
    """
    A decorator to run a method in a separate QThread.
    It prevents concurrent execution for the same prefix and cleans up
    its own attributes after the thread finishes.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            thread_attr = f"_{prefix}_thread"
            worker_attr = f"_{prefix}_worker"

            existing_thread = getattr(self, thread_attr, None)
            if existing_thread and existing_thread.isRunning():
                logger.error(f"Worker com prefixo '{prefix}' já está sendo executado.")
                return None

            # The decorated function returns the worker to run.
            # If it returns None, it means there's no work to do.
            worker = func(self, *args, **kwargs)
            if not worker:
                return None

            if not isinstance(worker, QObject):
                logger.error(f"Função '{func.__name__}' não retornou um QObject (worker).")
                return None

            thread = QThread()
            setattr(self, thread_attr, thread)
            setattr(self, worker_attr, worker)
            
            worker.moveToThread(thread)

            # --- Connect signals for lifecycle management ---
            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)

            # This cleanup function is the key to avoiding the original RuntimeError
            def cleanup_references():
                logger.debug(f"Cleaning up thread/worker attributes for prefix '{prefix}'.")
                if hasattr(self, thread_attr):
                    delattr(self, thread_attr)
                if hasattr(self, worker_attr):
                    delattr(self, worker_attr)

            thread.finished.connect(cleanup_references)
            
            thread.start()
            return worker
        return wrapper
    return decorator