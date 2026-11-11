"""Additive, versioned synchronization storage; legacy mappings stay intact."""
import os
import sqlite3


def migrate_sync_schema(conn, path):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 2:
        raise RuntimeError("Synchronization database is newer than this application")
    if version == 2:
        return
    if version == 1:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS SyncScope (id INTEGER PRIMARY KEY CHECK(id=1), scope TEXT NOT NULL)")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        return
    # Back up an existing installation using SQLite's WAL-aware backup API.
    if path != ":memory:" and conn.execute("SELECT 1 FROM DeliveryMapping UNION ALL SELECT 1 FROM DeliverymenMapping LIMIT 1").fetchone():
        backup_path = path + ".pre-sync-v1.db"
        if not os.path.exists(backup_path):
            with sqlite3.connect(backup_path) as backup:
                conn.backup(backup)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS SyncOrders (
            scope TEXT NOT NULL, internal_id TEXT NOT NULL,
            payload TEXT, request TEXT, submission TEXT NOT NULL DEFAULT 'QUEUED',
            cancellation TEXT NOT NULL DEFAULT 'NONE', external_id TEXT, job_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, remote_state TEXT,
            PRIMARY KEY(scope, internal_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS SyncEffects (
            scope TEXT NOT NULL, internal_id TEXT NOT NULL, effect_key TEXT NOT NULL,
            operation TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            UNIQUE(scope, internal_id, effect_key))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS SyncSource (
            scope TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(scope, source_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS SyncCheckpoints (
            scope TEXT PRIMARY KEY, cursor INTEGER NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS sync_orders_pending ON SyncOrders(scope, submission, retry_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS sync_effects_pending ON SyncEffects(scope, state, sequence)")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    migrate_sync_schema(conn, path)
