# tests/conftest.py
import pytest
import os
from pathlib import Path

# Import-time application configuration must never load workstation credentials.
os.environ["VELIDE_CONFIG_PATH"] = str(Path(__file__).parent / "fixtures" / "test_config.yml")

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

    yield {
        "tps": tps,
        "sqlite": sqlite_service,
        "db_manager": db_manager,
        "db_path": db_path_str,
    }
    # Do not destroy Qt service receivers while cleanup/write workers still emit.
    tps._cleanup_service.stop_routine()
    sqlite_service.thread_pool.waitForDone()
    from PyQt5.QtWidgets import QApplication
    QApplication.processEvents()


@pytest.fixture
def farmax_stack():
    import sqlite3
    from datetime import time
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool
    from connectors.farmax.farmax_repository import FarmaxRepository
    sqlite3.register_adapter(time, lambda value: value.isoformat())
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    sql = (Path(__file__).parent / "fixtures" / "farmax_observer.sql").read_text(encoding="utf-8")
    with engine.begin() as conn:
        for statement in sql.split(";"):
            if statement.strip():
                conn.execute(text(statement))
    repo = FarmaxRepository(engine)
    yield repo, engine
    engine.dispose()


@pytest.fixture
def remote_state():
    def make(status="COMPLETED", revision="1", route="route", **changes):
        state = dict(externalOrderId="123", found=True, revision=revision, jobId="job", importStatus="COMPLETED",
                     deliveryId="remote", deliveryStatus=status, deleted=False, cancellation="NONE",
                     execution=dict(id=route, courierId="courier", startedAt="2026-11-11T15:00:00Z",
                                    completedAt="2026-11-11T16:00:00Z" if status == "COMPLETED" else None))
        state.update(changes)
        return state
    return make
