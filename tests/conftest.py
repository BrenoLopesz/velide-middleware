# tests/conftest.py
import pytest
import os
from services.sqlite_service import SQLiteService
from services.tracking_persistence_service import TrackingPersistenceService
from api.sqlite_manager import SQLiteManager

@pytest.fixture
def persistence_stack(tmp_path, qtbot):
    """
    Sets up the full stack: TPS -> SQLiteService -> Real Temp DB File.
    Returns a dictionary for easy access to components.
    """
    # 1. Create a temporary file path. 
    # 'tmp_path' is a pathlib object provided by pytest that deletes itself after tests.
    db_file = tmp_path / "test_farmax.db"
    db_path_str = str(db_file)

    # 2. Initialize the SQLiteService with this file path
    sqlite_service = SQLiteService(db_path=db_path_str)
    
    # 3. Initialize the TPS with the sqlite service
    tps = TrackingPersistenceService(sqlite_service=sqlite_service)

    # 4. Helper: We need a way to READ the DB synchronously in our tests
    # to verify that TPS actually wrote to the disk.
    db_manager = SQLiteManager(db_path=db_path_str)

    return {
        "tps": tps,
        "sqlite": sqlite_service,
        "db_manager": db_manager,
        "db_path": db_path_str
    }