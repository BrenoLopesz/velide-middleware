"""Serialized, additive synchronization migrations; legacy mappings stay intact."""
import os
import sqlite3
from pathlib import Path


def migrate_sync_schema(conn, path):
    # The version decision belongs inside the write lock. Another first opener
    # may have completed the entire upgrade while this connection was waiting.
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 5:
            raise RuntimeError("Synchronization database is newer than this application")
        if version == 0:
            if path != ":memory:" and conn.execute("SELECT 1 FROM DeliveryMapping UNION ALL SELECT 1 FROM DeliverymenMapping LIMIT 1").fetchone():
                backup_path = path + ".pre-sync-v1.db"
                if not os.path.exists(backup_path):
                    # A separate reader can back up committed WAL state while
                    # this connection excludes new writers. Backing up through
                    # the connection holding a write transaction would hang.
                    source = sqlite3.connect(Path(path).absolute().as_uri()+"?mode=ro", uri=True)
                    target = sqlite3.connect(backup_path)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                        source.close()
            conn.execute("""CREATE TABLE IF NOT EXISTS SyncOrders (
                scope TEXT NOT NULL, internal_id TEXT NOT NULL, payload TEXT, request TEXT,
                submission TEXT NOT NULL DEFAULT 'QUEUED', cancellation TEXT NOT NULL DEFAULT 'NONE',
                external_id TEXT, job_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, last_error TEXT, remote_state TEXT,
                PRIMARY KEY(scope,internal_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS SyncEffects (
                scope TEXT NOT NULL, internal_id TEXT NOT NULL, effect_key TEXT NOT NULL,
                operation TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(scope,internal_id,effect_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS SyncSource (
                scope TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,last_error TEXT,PRIMARY KEY(scope,source_id))""")
            conn.execute("CREATE TABLE IF NOT EXISTS SyncCheckpoints(scope TEXT PRIMARY KEY,cursor INTEGER NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS sync_orders_pending ON SyncOrders(scope,submission,retry_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS sync_effects_pending ON SyncEffects(scope,state,sequence)")
        if version < 2:
            conn.execute("CREATE TABLE IF NOT EXISTS SyncScope(id INTEGER PRIMARY KEY CHECK(id=1),scope TEXT NOT NULL)")
        if version < 3:
            for statement in [
                "ALTER TABLE SyncSource ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE SyncSource ADD COLUMN retry_at REAL NOT NULL DEFAULT 0",
                "ALTER TABLE SyncOrders ADD COLUMN effect_generation INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE SyncOrders ADD COLUMN effect_authority TEXT",
                "ALTER TABLE SyncOrders ADD COLUMN remote_revision TEXT",
                "ALTER TABLE SyncOrders ADD COLUMN source_review TEXT",
                "ALTER TABLE SyncOrders ADD COLUMN authority_conflict_revision TEXT",
                "ALTER TABLE SyncEffects ADD COLUMN generation INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE SyncEffects ADD COLUMN observation TEXT",
                "ALTER TABLE SyncEffects ADD COLUMN observed_at TEXT",
                "ALTER TABLE SyncScope ADD COLUMN legacy_store_id TEXT",
                "ALTER TABLE SyncScope ADD COLUMN source_identity TEXT",
                "ALTER TABLE SyncScope ADD COLUMN farmax_branch TEXT",
            ]:
                conn.execute(statement)
            conn.execute("UPDATE SyncEffects SET state='SUPERSEDED',last_error='Fresh authoritative observation required after upgrade' WHERE state='PENDING'")
        if version < 4:
            conn.execute("ALTER TABLE SyncScope ADD COLUMN source_timezone TEXT")
            conn.execute("ALTER TABLE SyncOrders ADD COLUMN source_receipt_raw TEXT")
        if version < 5:
            conn.execute("ALTER TABLE SyncOrders ADD COLUMN add_attempts INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE SyncOrders ADD COLUMN delete_attempts INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE SyncOrders ADD COLUMN farmax_progress TEXT")
            conn.execute("ALTER TABLE SyncOrders ADD COLUMN source_context TEXT")
            conn.execute("UPDATE SyncOrders SET add_attempts=attempts WHERE external_id IS NULL")
            conn.execute("""CREATE TABLE SyncFarmaxEvents (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                internal_id TEXT NOT NULL, evidence TEXT NOT NULL,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("PRAGMA user_version=5")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
