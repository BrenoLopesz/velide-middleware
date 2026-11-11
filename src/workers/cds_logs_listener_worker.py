import json
import os
import queue
import threading
import time

from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class CdsLogsListenerSignals(QObject):
    new_order = pyqtSignal(object)
    finished = pyqtSignal()
    error = pyqtSignal(str)


class _NewFileHandler(FileSystemEventHandler):
    def __init__(self, enqueue):
        super().__init__()
        self.enqueue = enqueue

    def on_created(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.enqueue(event.dest_path)


class CdsLogsListenerWorker(QRunnable):
    """Watch plus periodic scans; durable identity is acknowledged by the strategy."""
    def __init__(self, folder_path):
        super().__init__()
        self.folder_path = folder_path
        self.signals = CdsLogsListenerSignals()
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._observer = None

    def enqueue(self, path):
        name = os.path.basename(path).lower()
        if name.startswith("ent") and name.endswith(".json"):
            self._queue.put(path)

    def _read_file(self, path):
        for attempt in range(5):
            if self._stop.is_set():
                return
            try:
                before = os.stat(path)
                if self._stop.wait(0.1 * (attempt + 1)):
                    return
                with open(path, "r", encoding="windows-1252") as stream:
                    data = json.load(stream)
                after = os.stat(path)
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    continue
                if not isinstance(data, dict):
                    raise ValueError("Expected a CDS order object")
                data["_source_file"] = os.path.basename(path).lower()
                self.signals.new_order.emit(data)
                return
            except (OSError, ValueError) as error:
                if attempt == 4:
                    self.signals.error.emit("CDS {}: {}".format(os.path.basename(path), error))
                # The periodic scan tries again; source files are never deleted.

    def run(self):
        try:
            if not os.path.isdir(self.folder_path):
                raise ValueError("Diretório CDS inválido")
            self._observer = Observer()
            self._observer.schedule(_NewFileHandler(self.enqueue), self.folder_path, recursive=False)
            self._observer.start()
            scan_at = 0
            while not self._stop.is_set():
                if time.monotonic() >= scan_at:
                    with os.scandir(self.folder_path) as entries:
                        for entry in entries:
                            if self._stop.is_set():
                                break
                            if entry.is_file():
                                self.enqueue(entry.path)
                    scan_at = time.monotonic() + 30
                try:
                    self._read_file(self._queue.get(timeout=0.25))
                except queue.Empty:
                    pass
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            if self._observer:
                self._observer.stop()
                self._observer.join()
            self.signals.finished.emit()

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
