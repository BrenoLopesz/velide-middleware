import pytest
from unittest.mock import MagicMock, patch
from PyQt5.QtCore import QThreadPool

# Adjust imports based on your actual file structure
from connectors.farmax.farmax_status_tracker import FarmaxStatusTracker, FarmaxTrackerConfig
from connectors.farmax.farmax_repository import FarmaxRepository
from services.tracking_persistence_service import TrackingPersistenceService
from models.farmax_models import FarmaxSale

# -----------------------------------------------------------------------------
# Fixtures: Setup Mocks
# -----------------------------------------------------------------------------

@pytest.fixture
def mock_repo():
    return MagicMock(spec=FarmaxRepository)

@pytest.fixture
def mock_persistence():
    return MagicMock(spec=TrackingPersistenceService)

@pytest.fixture
def mock_thread_pool():
    """
    Patches the global QThreadPool instance.
    This prevents actual thread spawning and allows us to verify .start() was called.
    """
    with patch.object(QThreadPool, 'globalInstance') as mock_pool_getter:
        mock_pool_instance = MagicMock()
        mock_pool_getter.return_value = mock_pool_instance
        yield mock_pool_instance

@pytest.fixture
def tracker(mock_repo, mock_persistence, mock_thread_pool):
    """
    Creates an instance of FarmaxStatusTracker with mocked dependencies.
    """
    # Small batch size to test batching logic easily
    config = FarmaxTrackerConfig(poll_interval_ms=1000, batch_size=2)
    tracker = FarmaxStatusTracker(mock_repo, mock_persistence, config)
    
    # Mock the internal logger to prevent cluttering test output and verify logging
    tracker._logger = MagicMock()
    
    # Mock the internal QTimer so we don't need a Qt Event Loop
    tracker._poll_timer = MagicMock()
    
    return tracker

# -----------------------------------------------------------------------------
# Tests: Public Interface (Start/Stop)
# -----------------------------------------------------------------------------

def test_start_initializes_loop(tracker):
    """Should set running flag, execute immediately, and start timer."""
    with patch.object(tracker, '_execute_poll_cycle') as mock_poll:
        tracker.start()
        
        assert tracker._is_running is True
        mock_poll.assert_called_once()  # Should run immediately
        tracker._poll_timer.start.assert_called_once_with(tracker._config.poll_interval_ms)

def test_start_ignores_if_already_running(tracker):
    """Should do nothing if called twice."""
    tracker._is_running = True
    tracker.start()
    tracker._poll_timer.start.assert_not_called()

def test_stop_halts_execution(tracker):
    """Should set running flag to False and stop timer."""
    tracker._is_running = True
    tracker.stop()
    
    assert tracker._is_running is False
    tracker._poll_timer.stop.assert_called_once()

# -----------------------------------------------------------------------------
# Tests: Polling Logic (_execute_poll_cycle)
# -----------------------------------------------------------------------------

def test_poll_cycle_aborts_if_not_running(tracker, mock_persistence):
    """Should return early if is_running is False."""
    tracker._is_running = False
    tracker._execute_poll_cycle()
    mock_persistence.get_active_monitored_ids.assert_not_called()

def test_poll_cycle_aborts_if_busy(tracker, mock_persistence):
    """Should return early if previous cycle is still processing."""
    tracker._is_running = True
    tracker._is_processing = True  # Simulate busy state
    
    tracker._execute_poll_cycle()
    
    mock_persistence.get_active_monitored_ids.assert_not_called()
    tracker._logger.debug.assert_called_with("Ciclo de rastreamento anterior ainda em andamento. Pulando...")

def test_poll_cycle_releases_lock_on_empty_queue(tracker, mock_persistence):
    """Should release processing lock if no IDs are returned."""
    tracker._is_running = True
    mock_persistence.get_active_monitored_ids.return_value = []

    tracker._execute_poll_cycle()

    assert tracker._is_processing is False
    tracker._logger.debug.assert_called_with("Nenhum pedido ativo para monitorar.")

@patch('connectors.farmax.farmax_status_tracker.FarmaxWorker') # Patch the worker class definition
def test_poll_cycle_batches_and_spawns_workers(mock_worker_cls, tracker, mock_persistence, mock_thread_pool):
    """
    Happy Path:
    1. Gets IDs [101, 102, 103].
    2. Batch size is 2.
    3. Should spawn 2 workers: one for [101, 102], one for [103].
    """
    tracker._is_running = True
    mock_persistence.get_active_monitored_ids.return_value = [101.0, 102.0, 103.0]
    
    # Mock the worker instance returned by the class method
    mock_worker_instance = MagicMock()
    mock_worker_cls.for_fetch_sales_statuses_by_id.return_value = mock_worker_instance

    # Act
    tracker._execute_poll_cycle()

    # Assert
    assert tracker._is_processing is True # Lock should be held while workers run
    
    # Check Worker instantiation
    assert mock_worker_cls.for_fetch_sales_statuses_by_id.call_count == 2
    
    # Verify Batch 1
    call_1 = mock_worker_cls.for_fetch_sales_statuses_by_id.call_args_list[0]
    assert call_1[1]['cd_vendas'] == (101.0, 102.0)
    
    # Verify Batch 2
    call_2 = mock_worker_cls.for_fetch_sales_statuses_by_id.call_args_list[1]
    assert call_2[1]['cd_vendas'] == (103.0,)

    # Verify ThreadPool started
    assert mock_thread_pool.start.call_count == 2
    mock_thread_pool.start.assert_called_with(mock_worker_instance)

def test_poll_cycle_resilience_to_exceptions(tracker, mock_persistence):
    """If DB query fails, lock must be released to allow next cycle."""
    tracker._is_running = True
    mock_persistence.get_active_monitored_ids.side_effect = Exception("DB Connection Lost")

    tracker._execute_poll_cycle()

    assert tracker._is_processing is False # Vital: Lock must be released
    tracker._logger.error.assert_called()

# -----------------------------------------------------------------------------
# Tests: Callbacks (_on_statuses_retrieved)
# -----------------------------------------------------------------------------

def test_on_statuses_retrieved_handles_cancellation(tracker, mock_persistence):
    """Status 'C' should trigger cancellation signal + persistence update."""
    tracker._is_running = True
    tracker.order_cancelled = MagicMock()
    
    # Setup Data
    sale_cancelled = FarmaxSale(cd_venda=123.0, status='C')
    mock_persistence.get_external_id.return_value = "EXT_REF_001"

    # Act
    tracker._on_statuses_retrieved([sale_cancelled])

    # Assert
    mock_persistence.get_external_id.assert_called_with("123.0")
    tracker.order_cancelled.emit.assert_called_with("123.0", "EXT_REF_001")
    mock_persistence.mark_as_cancelled.assert_called_with(123.0)

def test_on_statuses_retrieved_handles_delivery(tracker, mock_persistence):
    """Status 'F' should update persistence (stop tracking) but not emit cancel."""
    tracker._is_running = True
    tracker.order_cancelled = MagicMock()
    
    # Setup Data
    sale_finished = FarmaxSale(cd_venda=456.0, status='F')

    # Act
    tracker._on_statuses_retrieved([sale_finished])

    # Assert
    tracker.order_cancelled.emit.assert_not_called()
    mock_persistence.mark_as_finished.assert_called_with(456.0)

def test_on_statuses_retrieved_ignores_active_orders(tracker, mock_persistence):
    """Status 'A' (Active) should result in no changes."""
    tracker._is_running = True
    tracker.order_cancelled = MagicMock()
    
    sale_active = FarmaxSale(cd_venda=789.0, status='A')

    tracker._on_statuses_retrieved([sale_active])

    tracker.order_cancelled.emit.assert_not_called()
    mock_persistence.mark_as_cancelled.assert_not_called()
    mock_persistence.mark_as_finished.assert_not_called()

def test_on_statuses_retrieved_handles_malformed_data(tracker):
    """Should emit error signal if data processing crashes."""
    tracker._is_running = True
    tracker.error_occurred = MagicMock()
    
    # Passing an object that doesn't have .id or .status will raise AttributeError
    tracker._on_statuses_retrieved([object()]) 

    tracker.error_occurred.emit.assert_called()
    tracker._logger.error.assert_called()

def test_on_worker_error_logs_only(tracker):
    """Worker error should log but not crash execution."""
    tracker._on_worker_error("Some SQL Error")
    tracker._logger.error.assert_called_with("Erro no worker de status: Some SQL Error")

def test_set_processing_finished(tracker):
    """Ensures the lock is released when signal is received."""
    tracker._is_processing = True
    tracker._set_processing_finished()
    assert tracker._is_processing is False

# -----------------------------------------------------------------------------
# Tests: Helper Logic (Parametrized)
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("status_code, expected_result", [
    ('C', True),
    ('c', True),      # Case insensitive
    ('D', True),      # Devolvido
    ('A', False),
    ('F', False),
    ('', False),
    (None, False),
])
def test_is_cancelled_logic(tracker, status_code, expected_result):
    assert tracker._is_cancelled(status_code) == expected_result

@pytest.mark.parametrize("status_code, expected_result", [
    ('F', True),
    ('E', True),
    ('ENTREGUE', True),
    ('FINALIZADO', True),
    ('finalizado', True), # Case insensitive
    ('C', False),
    ('A', False),
    (None, False),
])
def test_is_finished_logic(tracker, status_code, expected_result):
    assert tracker._is_finished(status_code) == expected_result