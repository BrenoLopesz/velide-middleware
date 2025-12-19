import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from unittest.mock import MagicMock, call

# Import your classes (adjust paths as necessary)
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor, FarmaxIngestorConfig
from connectors.farmax.farmax_worker import FarmaxWorker

# --- Mocks ---

class MockSignals(QObject):
    """Mimics the worker.signals object"""
    success = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

class MockWorker(QObject):
    """Mimics the FarmaxWorker"""
    def __init__(self):
        super().__init__()
        self.signals = MockSignals()

@pytest.fixture
def mock_deps(mocker):
    """Mocks the repository, persistence, and threadpool."""
    repo = mocker.MagicMock()
    persistence = mocker.MagicMock()
    
    # Mock QThreadPool to prevent actual threading and count calls
    mock_thread_pool = mocker.patch("PyQt5.QtCore.QThreadPool.globalInstance")
    pool_instance = mock_thread_pool.return_value
    pool_instance.start = mocker.MagicMock()
    
    return {
        "repo": repo,
        "persistence": persistence,
        "pool": pool_instance
    }

@pytest.fixture
def ingestor(mock_deps, qtbot):
    """Creates the ingestor instance attached to qtbot."""
    ingestor = FarmaxDeliveryIngestor(
        repository=mock_deps["repo"],
        persistence=mock_deps["persistence"],
        config=FarmaxIngestorConfig(poll_interval_ms=1000)
    )
    return ingestor

# --- The Tests ---

def test_race_condition_prevention_during_long_poll(ingestor, mock_deps, mocker):
    """
    PROOF: Ensures that if a poll cycle is running, a second trigger is ignored.
    """
    # 1. Setup the Mock Worker to return our controllable object
    mock_worker = MockWorker()
    
    # We mock the factory method used in Step 1
    mocker.patch.object(
        FarmaxWorker, 
        'for_fetch_recent_changes', 
        return_value=mock_worker
    )

    ingestor.start()
    
    # Check that start() triggered the first poll immediately
    # The pool should have been called ONCE
    assert mock_deps["pool"].start.call_count == 1
    
    # 2. SIMULATE THE RACE
    # The worker has NOT emitted 'success' yet. The flag _is_processing_cycle should be True.
    # We manually force the timer's callback (as if 30s passed).
    ingestor._execute_poll_cycle()
    
    # 3. PROOF
    # The pool.start count should STILL be 1. 
    # If the race condition existed, this would be 2.
    assert mock_deps["pool"].start.call_count == 1, "Race Condition detected: Overlapping cycle started!"

    # 4. Finish the first cycle naturally
    # We simulate the worker finishing successfully with empty logs
    mock_worker.signals.success.emit([]) 
    
    # 5. Verify Lock Release
    # Now that the first cycle finished, the next trigger SHOULD work.
    ingestor._execute_poll_cycle()
    
    # Count should now increment to 2
    assert mock_deps["pool"].start.call_count == 2, "Lock was not released after cycle finished!"

def test_deadlock_prevention_on_poll_error(ingestor, mock_deps, mocker):
    """
    PROOF: Ensures the 'Busy Flag' is released even if the database fails (Fixing the Deadlock).
    """
    mock_worker = MockWorker()
    mocker.patch.object(
        FarmaxWorker, 
        'for_fetch_recent_changes', 
        return_value=mock_worker
    )

    ingestor.start()
    assert mock_deps["pool"].start.call_count == 1
    
    # 1. Simulate a Crash/Error in the worker
    error_msg = "Database Connection Lost"
    mock_worker.signals.error.emit(error_msg)
    
    # 2. Attempt a new cycle
    ingestor._execute_poll_cycle()
    
    # 3. PROOF
    # If the bug (deadlock) existed, call_count would still be 1 because the flag wasn't reset.
    # It should be 2 now.
    assert mock_deps["pool"].start.call_count == 2, "Deadlock detected: Lock not released on error!"

def test_race_condition_during_retry_wait(ingestor, mock_deps, mocker):
    """
    PROOF: Ensures that while waiting for a retry timer, we are still 'locked'.
    """
    # Setup for Step 2 (Fetching Details)
    mock_worker_logs = MockWorker()
    mock_worker_details = MockWorker()
    
    mocker.patch.object(FarmaxWorker, 'for_fetch_recent_changes', return_value=mock_worker_logs)
    mocker.patch.object(FarmaxWorker, 'for_fetch_deliveries_by_id', return_value=mock_worker_details)
    
    # Mock the mapper to return some dummy IDs to trigger step 2
    mocker.patch("connectors.farmax.farmax_mapper.FarmaxMapper.filter_new_insert_ids", return_value=[123])
    
    # Start and finish Step 1
    ingestor.start()
    mock_worker_logs.signals.success.emit([mocker.MagicMock(id=100)]) 
    
    # Now Step 2 (Fetch Details) has started. pool count = 2 (1 for log, 1 for details)
    assert mock_deps["pool"].start.call_count == 2
    
    # 1. Fail Step 2 (Trigger Retry Logic)
    # This stops the main timer and starts the retry timer.
    mock_worker_details.signals.error.emit("Network Error")
    
    # 2. Simulate Main Timer Misfire
    # Even though the worker thread finished (failed), we are logically "busy" waiting for retry.
    # If we call poll now, it should be blocked.
    ingestor._execute_poll_cycle()
    
    # 3. PROOF
    # Should still be 2. We don't want a new log poll while retrying an old batch.
    assert mock_deps["pool"].start.call_count == 2, "Race Condition: Polled new logs while retrying old batch!"