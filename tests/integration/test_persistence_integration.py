# tests/integration/test_persistence_flow.py
import pytest
from api.sqlite_manager import DeliveryStatus

class TestPersistenceIntegration:

    def test_register_new_delivery_writes_to_disk(self, persistence_stack, qtbot):
        """
        Tests that register_new_delivery updates the Cache immediately
        AND persists to SQLite asynchronously.
        """
        # --- ARRANGE ---
        tps = persistence_stack["tps"]
        sqlite_service = persistence_stack["sqlite"]
        db_manager = persistence_stack["db_manager"]

        # --- ACT ---
        # 1. Setup a "Blocker" to wait for the background thread to finish.
        # We listen to the signal 'add_delivery_result' from SQLiteService.
        with qtbot.waitSignal(sqlite_service.add_delivery_result, timeout=2000) as blocker:
            
            # 2. Trigger the action
            tps.register_new_delivery(
                internal_id="555.0", # Pass a float/string to test normalization
                external_id="UUID-999",
                status=DeliveryStatus.ADDED
            )

        # --- ASSERT (Synchronous Cache) ---
        # The cache should update instantly, even if the thread is slow
        assert tps.get_current_status("555") == DeliveryStatus.ADDED
        assert tps.get_external_id("555") == "UUID-999"

        # --- ASSERT (Asynchronous Disk) ---
        # The blocker context manager ensures we only reach here AFTER 
        # the worker thread has emitted 'add_delivery_result'.
        
        # Verify the worker claimed success (signal emitted True)
        assert blocker.args[0] is True 

        # Verify the actual data on disk using a fresh connection
        with db_manager as db:
            result = db.get_delivery_by_internal_id("555")
            
        assert result is not None
        external_id, status = result
        assert external_id == "UUID-999"
        assert status == DeliveryStatus.ADDED

    def test_hydration_loads_data_from_disk(self, persistence_stack, qtbot):
        """
        Tests that .initialize() reads existing data from disk and populates the cache.
        """
        # --- ARRANGE ---
        tps = persistence_stack["tps"]
        db_manager = persistence_stack["db_manager"]

        # 1. Seed the database manually (Backdoor setup)
        with db_manager as db:
            db.add_delivery_mapping(
                external_id="OLD-UUID",
                internal_id="100",
                status=DeliveryStatus.PENDING
            )
            
        # Ensure cache is empty before we start
        assert tps.get_current_status("100") is None

        # --- ACT ---
        # We wait for the 'hydrated' signal which TPS emits when done
        with qtbot.waitSignal(tps.hydrated, timeout=2000):
            tps.initialize()

        # --- ASSERT ---
        # Data should now be in the in-memory cache
        assert tps.get_current_status("100") == DeliveryStatus.PENDING