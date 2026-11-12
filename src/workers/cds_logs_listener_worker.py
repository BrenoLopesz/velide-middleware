import json
import os
from collections import OrderedDict
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
    def __init__(self, enqueue, forget=None):
        super().__init__()
        self.enqueue = enqueue
        self.forget = forget

    def on_created(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.enqueue(event.dest_path)
            if self.forget:
                self.forget(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self.forget:
            self.forget(event.src_path)


class CdsLogsListenerWorker(QRunnable):
    """Watch plus periodic scans; durable identity is acknowledged by the strategy."""
    def __init__(self, folder_path):
        super().__init__()
        self.folder_path = folder_path
        self.signals = CdsLogsListenerSignals()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._urgent = OrderedDict()
        self._scan = OrderedDict()
        self._seen = {}
        self._inflight = {}
        self._serial = 0
        self._observer = None

    def enqueue(self, path, priority=True):
        name = os.path.basename(path).lower()
        if name.startswith("ent") and name.endswith(".json"):
            try:
                stat = os.stat(path)
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                return
            with self._lock:
                if priority:
                    self._seen.pop(path, None)
                if not priority and self._seen.get(path) == signature:
                    return
                pending = self._inflight.get(path)
                if not priority and pending and pending[0] == signature and pending[1] > time.monotonic():
                    return
                if priority:
                    self._scan.pop(path, None)
                    self._urgent[path] = signature
                elif path not in self._urgent:
                    self._scan[path] = signature
                # Files are the durable backlog. A later bounded scan revisits
                # overflow; changed/new event paths take priority over scan work.
                while len(self._scan) + len(self._urgent) > 1024:
                    if self._scan:
                        self._scan.popitem(last=True)
                    else:
                        self._urgent.popitem(last=False)

    def forget(self, path):
        with self._lock:
            for mapping in (self._scan, self._urgent, self._seen, self._inflight):
                mapping.pop(path, None)

    def _scan_has_capacity(self):
        with self._lock:
            return len(self._scan) + len(self._urgent) < 512

    def acknowledge(self, observation):
        path, size, modified = observation[:3]
        ticket = observation[3] if len(observation) > 3 else None
        with self._lock:
            if ticket is not None and (path not in self._inflight or self._inflight[path][2] != ticket):
                return
            self._seen[path] = (size, modified)
            self._inflight.pop(path, None)

    def _next_path(self):
        with self._lock:
            pending = self._urgent if self._urgent else self._scan
            if not pending:
                return None
            path, signature = pending.popitem(last=False)
            self._seen.pop(path, None)
            self._serial += 1
            self._inflight[path] = (signature, time.monotonic() + 30, self._serial)
            return path

    def _read_file(self, path):
        with self._lock:
            self._serial += 1
            ticket = self._serial
            self._inflight[path] = (None, time.monotonic()+30, ticket)
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
                data["_source_observation"] = (path, after.st_size, after.st_mtime_ns, ticket)
                with self._lock:
                    self._inflight[path] = ((after.st_size, after.st_mtime_ns), time.monotonic()+30, ticket)
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
            self._observer.schedule(_NewFileHandler(self.enqueue, self.forget), self.folder_path, recursive=False)
            self._observer.start()
            scan_at, entries = 0, None
            while not self._stop.is_set():
                if entries is None and time.monotonic() >= scan_at:
                    entries = os.scandir(self.folder_path)
                if entries is not None:
                    for _ in range(100):
                        if not self._scan_has_capacity():
                            break  # Resume this iterator rather than discarding its tail.
                        try:
                            entry = next(entries)
                        except StopIteration:
                            entries.close()
                            entries = None
                            scan_at = time.monotonic() + 30
                            break
                        if entry.is_file():
                            self.enqueue(entry.path, priority=False)
                path = self._next_path()
                if path:
                    self._read_file(path)
                else:
                    self._stop.wait(0.05)
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            if 'entries' in locals() and entries is not None:
                entries.close()
            if self._observer:
                self._observer.stop()
                self._observer.join()
            self.signals.finished.emit()

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
