import json
import logging
import os
from collections import OrderedDict
from datetime import datetime
import threading
import time
import uuid

from pydantic import TypeAdapter
from PyQt5.QtCore import QObject, pyqtSignal, QRunnable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


_RECEIPT = TypeAdapter(datetime)


def _cds_name(path):
    name = os.path.basename(path).lower()
    return name if name.startswith("ent") and name.endswith(".json") else None


def _receipt(data):
    try:
        return _RECEIPT.validate_python(data["horario_pedido"]).isoformat()
    except (ValueError, TypeError, KeyError):
        return None


def _pending_names(pending_files):
    pending = set()
    for name in pending_files:
        if not _cds_name(name) or os.path.basename(name) != name:
            raise ValueError("Informe apenas nomes de arquivos ent*.json pendentes do CDS.")
        pending.add(name.lower())
    return pending


def prepare_cds_observation_baseline(folder_path, baseline_path, pending_files=()):
    """Persist the first observation boundary, preferably before an old install stops.

    Existing files are *unobserved*, not declared delivered. The legacy watcher
    only handled newly created files. A reviewed pending basename may be included
    explicitly. Subsequent calls load the same boundary, never take a new snapshot.
    Keep this sidecar with the installation's database, including during upgrades.
    """
    folder = os.path.normcase(os.path.abspath(folder_path))
    if os.path.exists(baseline_path):
        with open(baseline_path, encoding="utf-8") as stream:
            baseline = json.load(stream)
        if (not isinstance(baseline, dict) or baseline.get("version") != 1 or baseline.get("folder") != folder
                or not isinstance(baseline.get("unobserved_files"), dict)):
            raise ValueError("O marco de observação CDS não corresponde à pasta configurada.")
        for name, entry in baseline["unobserved_files"].items():
            if (_cds_name(name) != name or os.path.basename(name) != name
                    or not isinstance(entry, dict)
                    or not isinstance(entry.get("signature"), list) or len(entry["signature"]) != 2
                    or not all(isinstance(value, int) for value in entry["signature"])
                    or (entry.get("receipt") is not None
                        and (not isinstance(entry["receipt"], str) or _receipt({"horario_pedido": entry["receipt"]}) != entry["receipt"]))):
                raise ValueError("O marco de observação CDS está inválido.")
        return baseline

    pending = _pending_names(pending_files)
    baseline = {"version": 1, "folder": folder, "observed_at_ns": time.time_ns(), "unobserved_files": {}}
    with os.scandir(folder_path) as entries:
        for entry in entries:
            name = _cds_name(entry.name)
            if not name or name in pending or not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            # On Windows ctime is creation time, including copies with an old
            # mtime. Files born while the snapshot is being built remain eligible.
            if os.name == "nt" and stat.st_ctime_ns > baseline["observed_at_ns"]:
                continue
            receipt = None
            try:
                with open(entry.path, encoding="windows-1252") as stream:
                    receipt = _receipt(json.load(stream))
                after = os.stat(entry.path)
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    receipt = None
            except (OSError, ValueError):
                pass  # An unreadable pre-observation file is not proven new work.
            baseline["unobserved_files"][name] = {
                "signature": [stat.st_size, stat.st_mtime_ns], "receipt": receipt,
            }
    parent = os.path.dirname(os.path.abspath(baseline_path))
    os.makedirs(parent, exist_ok=True)
    temporary = str(baseline_path) + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(baseline, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, baseline_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # Only our temporary sidecar, never a CDS file.
    return baseline


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
    """Watch and rescan files after the durable legacy-compatible boundary.

    Pre-boundary reprints/edits with the same receipt stay excluded. A reused
    filename with a new receipt is eligible. A pre-boundary unreadable receipt
    remains excluded by name; it needs explicit pending-file selection at cutover.
    Post-boundary edits/reprints go through the journal's identity/conflict policy.
    """
    def __init__(self, folder_path, baseline_path=None, pending_files=()):
        super().__init__()
        self.folder_path = folder_path
        self.baseline_path = baseline_path or os.path.join(folder_path, ".velide-cds-observation.json")
        self.logger = logging.getLogger(__name__)
        self._baseline = {}
        # Explicit observation exceptions supplied by installation/migration.
        # Unresolved inventory entries require the journal's upgrade hold hook.
        # Pass overrides again on restart; they never rewrite an existing boundary.
        self._pending_files = _pending_names(pending_files)
        self._reported_errors = {}
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
        name = _cds_name(path)
        if name:
            try:
                stat = os.stat(path)
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                return
            previous = self._baseline.get(name) if name not in self._pending_files else None
            if previous and (previous["receipt"] is None or (not priority and tuple(previous["signature"]) == signature)):
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
            for mapping in (self._scan, self._urgent, self._seen, self._inflight, self._reported_errors):
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
                    raise ValueError("O arquivo CDS deve conter os dados de um pedido.")
                data["_source_file"] = os.path.basename(path).lower()
                data["_source_observation"] = (path, after.st_size, after.st_mtime_ns, ticket)
                with self._lock:
                    self._inflight[path] = ((after.st_size, after.st_mtime_ns), time.monotonic()+30, ticket)
                previous = self._baseline.get(data["_source_file"]) if data["_source_file"] not in self._pending_files else None
                if previous and (previous["receipt"] is None or previous["receipt"] == _receipt(data)):
                    self.acknowledge(data["_source_observation"])
                    return
                self._reported_errors.pop(path, None)
                self.signals.new_order.emit(data)
                return
            except (OSError, ValueError) as error:
                if attempt == 4 and self._reported_errors.get(path) != str(error):
                    self._reported_errors[path] = str(error)
                    message = ("Não foi possível ler o pedido CDS no arquivo {}. "
                               "Verifique se o CDS terminou de gravá-lo e se a pasta permite leitura. "
                               "A leitura será tentada novamente.").format(os.path.basename(path))
                    self.logger.exception("CDS %s: %s", path, error, extra={"user_message": message})
                    self.signals.error.emit(message)
                # The periodic scan tries again; source files are never deleted.

    def _prepare_baseline(self):
        exists = os.path.exists(self.baseline_path)
        baseline = prepare_cds_observation_baseline(self.folder_path, self.baseline_path, self._pending_files)
        self._baseline = baseline["unobserved_files"]
        if not exists and self._baseline:
            self.logger.info(
                "Monitoramento CDS preparado: %s arquivo(s) já estavam na pasta antes do início. "
                "Como na versão anterior, eles não serão importados automaticamente. "
                "Se houver pedidos pendentes entre eles, solicite a conferência ao suporte.", len(self._baseline))

    def run(self):
        try:
            if not os.path.isdir(self.folder_path):
                raise ValueError("Diretório CDS inválido")
            self._prepare_baseline()
            self._observer = Observer()
            self._observer.schedule(_NewFileHandler(self.enqueue, self.forget), self.folder_path, recursive=False)
            self._observer.start()
            self.logger.info("Monitoramento CDS iniciado. Aguardando novos pedidos na pasta: %s", self.folder_path)
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
            message = ("O monitoramento CDS foi interrompido. Verifique se a pasta CDS está disponível "
                       "e se a integração tem permissão para acessar seus arquivos locais. Depois, conecte novamente. "
                       "Se o problema continuar, consulte o suporte com os registros de diagnóstico.")
            self.logger.exception("CDS watcher stopped: %s", error, extra={"user_message": message})
            self.signals.error.emit(message)
        finally:
            if 'entries' in locals() and entries is not None:
                entries.close()
            if self._observer:
                self._observer.stop()
                if self._observer.is_alive():
                    self._observer.join()
            self.logger.info("Monitoramento CDS encerrado.")
            self.signals.finished.emit()

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()
