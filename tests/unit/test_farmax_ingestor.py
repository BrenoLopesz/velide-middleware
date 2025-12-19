import pytest
from unittest.mock import MagicMock, patch
from datetime import date, datetime

# Adjust imports to match your project structure
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor, FarmaxIngestorConfig
from connectors.farmax.farmax_repository import FarmaxRepository
from services.tracking_persistence_service import TrackingPersistenceService
from models.farmax_models import DeliveryLog, FarmaxDelivery, FarmaxAction
from models.velide_delivery_models import Order

# --- Fixtures ---

@pytest.fixture
def mock_repo():
    """Mocks the interface of FarmaxRepository."""
    repo = MagicMock(spec=FarmaxRepository)
    # SAFETY NET: Default these methods to return empty lists so 
    # unintentional calls don't crash with "max() arg is empty"
    repo.fetch_recent_changes.return_value = []
    repo.fetch_recent_changes_by_id.return_value = []
    repo.fetch_deliveries_by_id.return_value = []
    return repo

@pytest.fixture
def mock_persistence():
    """Mocks the interface of TrackingPersistenceService."""
    service = MagicMock(spec=TrackingPersistenceService)
    # Default behavior: assume nothing is tracked yet
    service.is_tracked.return_value = False
    service.reserve_id.return_value = True
    return service

@pytest.fixture
def ingestor(mock_repo, mock_persistence, qtbot):
    """
    Creates the Ingestor with dependencies mocked.
    Crucially, it patches the ThreadPool to run synchronousl within the test.
    """
    ingestor = FarmaxDeliveryIngestor(
        repository=mock_repo,
        persistence=mock_persistence,
        config=FarmaxIngestorConfig(poll_interval_ms=1000)
    )
    
    # --- MAGIC TRICK: Synchronous ThreadPool ---
    # We mock the thread pool inside the instance.
    # When .start(worker) is called, we immediately run worker.run().
    ingestor._thread_pool = MagicMock()
    
    def immediate_execution(worker):
        worker.run()
        
    ingestor._thread_pool.start.side_effect = immediate_execution
    
    return ingestor

# --- Tests ---

def test_initialization_state(ingestor):
    """Ensure the ingestor starts in a clean state."""
    assert ingestor._is_running is False
    assert ingestor._cursor.last_log_id is None
    assert ingestor._cursor.last_check_time is None

def test_start_process(ingestor, qtbot):
    """Check if start sets the time and triggers the first poll."""
    with qtbot.waitSignal(ingestor._poll_timer.timeout, timeout=1100):
        # We manually trigger the timeout behavior or check if start calls execute
        # Note: Your code calls _execute_poll_cycle() immediately inside start()
        ingestor.start()
    
    assert ingestor._is_running is True
    assert ingestor._cursor.last_check_time is not None
    assert ingestor._poll_timer.isActive()

def test_poll_cycle_no_logs(ingestor, mock_repo):
    """
    Scenario: The polling worker runs, but the repo returns empty list.
    Result: Nothing happens, cursor does not advance to ID mode.
    """
    ingestor.start()
    
    # Setup Mock: Return empty logs
    mock_repo.fetch_recent_changes.return_value = []
    
    # Trigger Cycle
    ingestor._execute_poll_cycle()
    
    # Assertions
    mock_repo.fetch_recent_changes.assert_called()
    assert ingestor._cursor.last_log_id is None # Still in Time-based mode

def test_poll_cycle_with_irrelevant_logs(ingestor, mock_repo):
    """
    Scenario: Logs exist (e.g., Updates), but Mapper says no NEW Inserts.
    Result: Cursor advances (commits), but no details are fetched.
    """
    ingestor.start()
    
    # Mock Logs
    logs = [DeliveryLog(id=100, action=FarmaxAction.UPDATE.value, cd_venda=500.0, logdate=datetime.now())]
    mock_repo.fetch_recent_changes.return_value = logs
    
    # Mock Mapper to return empty list (No new inserts)
    with patch('connectors.farmax.farmax_mapper.FarmaxMapper.filter_new_insert_ids') as mock_mapper:
        mock_mapper.return_value = []
        
        ingestor._execute_poll_cycle()
        
        # Verify cursor moved to ID 100
        assert ingestor._cursor.last_log_id == 100
        # Verify we did NOT try to fetch details
        mock_repo.fetch_deliveries_by_id.assert_not_called()

def test_poll_cycle_happy_path(ingestor, mock_repo, mock_persistence, qtbot):
    """
    Scenario: 
    1. Log finds a new Insert (ID 555).
    2. Ingestor fetches details for 555.
    3. Persistence reserves 555.
    4. Order is emitted.
    """
    ingestor.start()
    
    # 1. Setup Logs
    logs = [DeliveryLog(id=200, action=FarmaxAction.INSERT.value, cd_venda=555.0, logdate=datetime.now())]
    mock_repo.fetch_recent_changes.return_value = logs
    
    # 2. Setup Mapper to confirm it's new
    with patch('connectors.farmax.farmax_mapper.FarmaxMapper.filter_new_insert_ids') as mock_mapper:
        mock_mapper.return_value = [555.0]
        
        # 3. Setup Detail Fetch
        # Note: We use aliases in the constructor if populating by field name, 
        # or standard names if Pydantic config allows. 
        # Here we instantiate using the standard attribute names for clarity.
        fake_delivery = FarmaxDelivery(
            cd_venda=555.0,
            nome="John Doe",
            fone="5599999999",
            hora_saida=None,       # Not started yet
            bairro="Centro",
            tempendereco="Rua Principal, 100",
            tempreferencia="Ao lado da praça",
            data=date.today(),
            hora=datetime.now().time()
        )
        mock_repo.fetch_deliveries_by_id.return_value = [fake_delivery]
        
        # 4. Setup Mapper for Order conversion
        # We also create a valid Order object for the mock return
        fake_order = Order(
            customerName="John Doe",
            address="Rua Principal, 100",
            createdAt=datetime.now(),
            customerContact="5599999999",
            reference="Ao lado da praça",
            neighbourhood="Centro",
            internal_id="555" # Excluded from dump, but exists on object
        )
        with patch('connectors.farmax.farmax_mapper.FarmaxMapper.to_order') as mock_to_order:
            mock_to_order.return_value = fake_order

            # Watch for the signal
            with qtbot.waitSignal(ingestor.orders_received) as blocker:
                ingestor._execute_poll_cycle()
            
            # --- Assertions ---
            
            # 1. Check if Details were fetched
            mock_repo.fetch_deliveries_by_id.assert_called_with(cd_vendas=(555.0,))
            
            # 2. Check if Persistence was called
            mock_persistence.reserve_id.assert_called_with(555.0)
            
            # 3. Check Signal Payload
            assert blocker.args[0] == [fake_order]
            
            # 4. Check Cursor Commit
            assert ingestor._cursor.last_log_id == 200

def test_race_condition_reservation_fail(ingestor, mock_repo, mock_persistence, qtbot):
    """
    Scenario: We fetch details, but Persistence.reserve_id returns False 
    (meaning another thread or process already picked it up).
    """
    ingestor.start()
    
    # Log says new item
    mock_repo.fetch_recent_changes.return_value = [
        DeliveryLog(id=300, action=FarmaxAction.INSERT.value, cd_venda=777.0, logdate=datetime.now())
    ]
    
    with patch('connectors.farmax.farmax_mapper.FarmaxMapper.filter_new_insert_ids', return_value=[777.0]):
        fake_delivery = FarmaxDelivery(
            cd_venda=777.0,
            nome="Jane Doe",
            fone=None,
            hora_saida=None,
            bairro="Bairro Alto",
            tempendereco="Rua Secundária, 200",
            tempreferencia=None,
            data=date.today(),
            hora=datetime.now().time()
        )
        mock_repo.fetch_deliveries_by_id.return_value = [fake_delivery]
        
        # FAIL condition: Reservation fails
        mock_persistence.reserve_id.return_value = False
        
        # Run
        with qtbot.assertNotEmitted(ingestor.orders_received):
            ingestor._execute_poll_cycle()
            
        # Even though we didn't emit, we processed the logic without crashing.
        # The cursor SHOULD commit because we "handled" it (by ignoring it).
        assert ingestor._cursor.last_log_id == 300

def test_retry_mechanism(ingestor, mock_repo, qtbot):
    """
    Scenario: Fetching details fails (DB Error). Ingestor should schedule a retry.
    """
    ingestor.start()
    
    # 1. Setup Logs
    mock_repo.fetch_recent_changes.return_value = [
        DeliveryLog(id=400, action=FarmaxAction.INSERT.value, cd_venda=999.0, logdate=datetime.now())
    ]
    
    with patch('connectors.farmax.farmax_mapper.FarmaxMapper.filter_new_insert_ids', return_value=[999.0]):
        
        # 2. Make detail fetch fail
        mock_repo.fetch_deliveries_by_id.side_effect = Exception("DB Disconnect")
        
        # Run cycle
        ingestor._execute_poll_cycle()
        
        # Assertions
        assert ingestor._retry_count == 1
        assert ingestor._retry_timer.isActive()
        # Verify the main poll timer was stopped to prevent overlap
        assert not ingestor._poll_timer.isActive()
        
        # 3. Simulate Retry Success
        # Move time forward or manually trigger retry
        mock_repo.fetch_deliveries_by_id.side_effect = None # Fix error
        mock_repo.fetch_deliveries_by_id.return_value = [] # Return empty for simplicity
        
        ingestor._retry_timer.timeout.emit()
        
        # Should be back to normal
        assert ingestor._retry_count == 0
        assert ingestor._cursor.last_log_id == 400