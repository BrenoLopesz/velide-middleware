import pytest
from api.sqlite_manager import SQLiteManager
from api.sync_journal import SyncJournal
from native_migration import migrate, prepare_legacy, processed_cds_ids


def test_manifest_upgrade_preserves_mappings_payloads_and_is_idempotent(tmp_path):
    path = str(tmp_path / "local.db")
    journal = SyncJournal(path, "legacy|Farmax")
    journal.accept("123", {"internal_id": "123"})
    journal.imported("123", "legacy-delivery")
    with SQLiteManager(path) as db:
        db.add_mapping("legacy-courier", "5")
    new_scope = "https://api.invalid/graphql|Farmax|00000000-0000-0000-0000-000000000001|00000000-0000-0000-0000-000000000002"
    manifest = {"version": 1, "old_scope": "legacy|Farmax", "new_scope": new_scope,
        "deliveries": [{"internal_id": "123", "legacy_delivery_id": "legacy-delivery", "native_delivery_id": "00000000-0000-0000-0000-000000000003", "state": "IMPORTED"}],
        "couriers": [{"local_id": "5", "legacy_id": "legacy-courier", "native_id": "00000000-0000-0000-0000-000000000004"}]}
    assert not migrate(path, manifest)["applied"]
    with SQLiteManager(path) as db:
        assert db.get_delivery_by_internal_id("123")[0] == "legacy-delivery"
    assert migrate(path, manifest, apply=True)["applied"]
    assert migrate(path, manifest, apply=True)["already_applied"]
    restored = SyncJournal(path, new_scope).restore()[0]
    assert restored["external_id"].endswith("0003")
    assert restored["payload"] is not None
    with SQLiteManager(path) as db:
        assert db.get_all_mappings()[0][0].endswith("0004")


def test_manifest_cannot_discard_unresolved_erp_effects(tmp_path, remote_state):
    path = str(tmp_path / "local.db")
    journal = SyncJournal(path, "legacy|Farmax")
    journal.native_states([remote_state()])
    with pytest.raises(ValueError, match="outstanding"):
        migrate(path, {"version": 1, "old_scope": "legacy|Farmax", "new_scope": "https://api.invalid/graphql|Farmax|00000000-0000-0000-0000-000000000001|00000000-0000-0000-0000-000000000002", "deliveries": [], "couriers": []})


def test_cds_historical_files_can_be_adopted_before_startup_scan(tmp_path):
    path = str(tmp_path / "cds.db")
    with SQLiteManager(path):
        pass
    manifest = {"version": 1, "old_scope": "legacy|CDS",
        "new_scope": "https://api.invalid/graphql|CDS|00000000-0000-0000-0000-000000000001|00000000-0000-0000-0000-000000000002",
        "deliveries": [], "couriers": [],
        "processed_cds_files": [{"filename": "ent1.json", "created_at": "2026-01-01T12:00:00"}]}
    prepare_legacy(path, manifest)
    migrate(path, manifest, apply=True)
    journal = SyncJournal(path, manifest["new_scope"])
    internal = processed_cds_ids(manifest)[0]
    assert journal.accept(internal, {"internal_id": internal})["submission"] == "TERMINAL"
    assert journal.claim(native=True) is None
