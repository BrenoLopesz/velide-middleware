"""Scope-bearing upgrade state, including additive journal versions."""


def scoped_upgrade_tables(conn):
    tables = ("SyncOrders", "SyncEffects", "SyncSource", "SyncCheckpoints", "SyncFarmaxEvents")
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return tuple(table for table in tables if table in present)
