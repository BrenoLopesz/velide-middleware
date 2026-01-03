import collections
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from workers.velide_worker import VelideWorker
from api.velide import Velide
from models.velide_delivery_models import Order
from typing import Deque, Optional

# Define Task Types
TASK_ADD = "ADD"
TASK_DELETE = "DELETE"

class DeliveryDispatcher(QObject):
    """
    Manages the queue of API tasks to ensure they are processed 
    sequentially or safely, decoupling concurrency from business logic.
    """
    # (internal_id, external_id)
    delivery_success = pyqtSignal(str, str) 
    # (internal_id, deleted_external_id)
    deletion_success = pyqtSignal(str, str)
    # (internal_id, error_message)
    task_failed = pyqtSignal(str, str)
    
    def __init__(self, thread_pool: QThreadPool):
        super().__init__()
        self._thread_pool = thread_pool
        self._queue: Deque[dict] = collections.deque()
        self._is_processing = False
        self._velide_api: Optional[Velide] = None

    def set_api(self, api: Velide):
        self._velide_api = api

    def queue_add(self, internal_id: str, order: Order):
        self._queue.append({'type': TASK_ADD, 'id': internal_id, 'payload': order})
        self._try_process_next()

    def queue_delete(self, internal_id: str, external_id: str):
        self._queue.append({'type': TASK_DELETE, 'id': internal_id, 'ext_id': external_id})
        self._try_process_next()

    def cancel_pending_add(self, internal_id: str) -> bool:
        """Removes an ADD task from queue if it hasn't run yet."""
        for i, task in enumerate(list(self._queue)):
            if task['type'] == TASK_ADD and task['id'] == internal_id:
                del self._queue[i]
                return True
        return False

    def _try_process_next(self):
        if self._is_processing or not self._queue:
            return

        self._is_processing = True
        task = self._queue.popleft()
        worker = self._create_worker(task)
        if not worker:
            self._on_worker_finished() # Skip invalid task
            return

        # We use lambdas to pass the 'internal_id' which the worker might not know about,
        # or we just rely on the worker returning the ID if it knows it.
        
        internal_id = task['id']

        if task['type'] == TASK_ADD:
            # Worker emits (api_response_dict)
            worker.signals.delivery_added.connect(
                lambda resp: self._handle_add_success(internal_id, resp)
            )
        elif task['type'] == TASK_DELETE:
            # Worker emits (deleted_id)
            worker.signals.delivery_deleted.connect(
                lambda ext_id: self.deletion_success.emit(internal_id, ext_id)
            )

        # Common Error Signal
        worker.signals.error.connect(
            lambda err: self.task_failed.emit(internal_id, err)
        )

        # Thread flow management
        worker.signals.finished.connect(self._on_worker_finished)

        self._thread_pool.start(worker)

    def _create_worker(self, task):
        task_type = task['type']

        if task_type == TASK_ADD:
            return VelideWorker.for_add_delivery(self._velide_api, task['payload'])

        elif task_type == TASK_DELETE:
            return VelideWorker.for_delete_delivery(self._velide_api, task['ext_id'])
        
        return None

    def _handle_add_success(self, internal_id, api_response):
        """Helper to extract ID before emitting."""
        external_id = api_response.get("id")
        if external_id:
            self.delivery_success.emit(internal_id, external_id)
        else:
            self.task_failed.emit(internal_id, "API returned success but no ID found.")

    def _on_worker_finished(self):
        self._is_processing = False
        self._try_process_next()