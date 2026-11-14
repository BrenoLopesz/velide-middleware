import json
import sqlite3

import pytest

from api.sqlite_manager import SQLiteManager
from local_upgrade import upgrade_installation
from native_migration import migrate, prepare_legacy
from upgrade_inventory import export_upgrade_inventory
from test_automatic_upgrade import installed, package_for, rows, supply
from utils.source_identity import configured_scope


NATIVE_SCOPE = "https://native.invalid/graphql|Farmax|10000000-0000-0000-0000-000000000001|10000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize("old_scope", ["legacy", "", "|Farmax", "legacy|", "legacy|Other", "legacy|Farmax|extra", None, 123, {}])
def test_malformed_old_scope_is_value_error_without_partial_database_changes(tmp_path, old_scope):
    path = str(tmp_path / "legacy.db")
    # For malformed strings, deliberately match the actual installed scope: the
    # old implementation got past its equality check and raised IndexError.
    with SQLiteManager(path) as database:
        conn = database._get_conn()
        conn.execute("INSERT INTO SyncScope(id,scope) VALUES(1,?)", (old_scope if isinstance(old_scope, str) else "legacy|Farmax",))
        conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('original','123','ADICIONADO')")
    before = rows(path, "SELECT * FROM DeliveryMapping")
    manifest = {"version": 1, "old_scope": old_scope, "new_scope": NATIVE_SCOPE, "deliveries": [], "couriers": []}
    for apply in (False, True):
        with pytest.raises(ValueError, match="Invalid legacy scope"):
            migrate(path, manifest, apply=apply)
    with pytest.raises(ValueError, match="Invalid legacy scope"):
        prepare_legacy(path, manifest)
    assert rows(path, "SELECT * FROM DeliveryMapping") == before
    assert not list(tmp_path.glob("*.native-*"))
    assert not rows(path, "SELECT name FROM sqlite_master WHERE name='SyncMigrations'")


def test_schema_v5_native_cutover_preserves_and_rescopes_farmax_event_history(tmp_path):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    scope = configured_scope(adopted.settings)
    evidence = json.dumps({"accepted": True, "source_context": {"branch_id": "7", "sale_id": "123"}, "cancelled_items": 1, "total_items": 2})
    with sqlite3.connect(str(database)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        conn.execute("INSERT INTO SyncFarmaxEvents(scope,internal_id,evidence) VALUES(?,'123',?)", (scope, evidence))
        conn.execute("UPDATE SyncOrders SET add_attempts=3,delete_attempts=1,farmax_progress=?", (evidence,))
    original = rows(database, "SELECT sequence,internal_id,evidence,observed_at FROM SyncFarmaxEvents")
    inventory = export_upgrade_inventory(config, tmp_path)
    assert inventory["farmax_events"][0]["evidence"]["cancelled_items"] == 1
    assert inventory["orders"][0]["add_attempts"] == 3
    package = package_for(config, database, adopted)
    package["inventory_state_sha256"] = inventory["state_sha256"]
    supply(config, package)
    migrated = upgrade_installation(config, tmp_path)
    assert migrated.native_applied, migrated.package_error
    assert rows(migrated.database_path, "SELECT sequence,internal_id,evidence,observed_at FROM SyncFarmaxEvents") == original
    assert rows(migrated.database_path, "SELECT DISTINCT scope FROM SyncFarmaxEvents") == [(configured_scope(migrated.settings),)]
    assert rows(migrated.database_path, "SELECT add_attempts,delete_attempts,farmax_progress FROM SyncOrders") == [(3, 1, evidence)]
    audit = json.loads(rows(migrated.database_path, "SELECT original FROM SyncMigrationOrders WHERE internal_id='123'")[0][0])
    assert audit["scope"] == scope and audit["external_id"] == "old-delivery"
    assert rows(database, "SELECT DISTINCT scope FROM SyncFarmaxEvents") == [(scope,)]


def test_new_v5_observation_after_export_defers_cutover_even_if_order_row_is_unchanged(tmp_path):
    config, database, _, _ = installed(tmp_path)
    inventory = export_upgrade_inventory(config, tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["inventory_state_sha256"] = inventory["state_sha256"]
    supply(config, package)
    original_config = config.read_bytes()
    with sqlite3.connect(str(database)) as conn:
        conn.execute("INSERT INTO SyncFarmaxEvents(scope,internal_id,evidence) VALUES(?,'123',?)",
                     (configured_scope(adopted.settings), json.dumps({"accepted": False, "reason": "Incomplete observation"})))
    result = upgrade_installation(config, tmp_path)
    assert not result.native_applied and "changed after inventory export" in result.package_error
    assert config.read_bytes() == original_config
    assert rows(database, "SELECT count(*) FROM SyncFarmaxEvents") == [(1,)]


def test_inventory_keeps_absolute_legacy_request_timestamp_and_sanitized_metadata(tmp_path):
    config, database, _, _ = installed(tmp_path)
    upgrade_installation(config, tmp_path)
    request = {"timestamp": 1794582000000, "offset": 0, "idempotencyKey": "123", "address": "Original address",
               "metadata": {"integrationName": "Farmax", "customerName": "Original customer", "access_token": "private-token"},
               "headers": {"Authorization": "Bearer private-token"}}
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET request=? WHERE internal_id='123'", (json.dumps(request),))
    exported = export_upgrade_inventory(config, tmp_path)["orders"][0]["request"]
    assert exported["timestamp"] == request["timestamp"] and exported["offset"] == 0
    assert exported["metadata"] == {"integrationName": "Farmax", "customerName": "Original customer"}
    assert "headers" not in exported and "private-token" not in json.dumps(exported)
