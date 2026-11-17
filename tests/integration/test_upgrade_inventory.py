import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import yaml

from api.sync_journal import SyncJournal
from local_upgrade import upgrade_installation
from upgrade_inventory import export_upgrade_inventory
from utils.source_identity import configured_scope
from test_automatic_upgrade import installed, package_for, receipt, rows, supply


def test_export_adopts_original_installation_without_config_changes_or_applying_waiting_package(tmp_path):
    config, database, _, _ = installed(tmp_path)
    before = config.read_bytes()
    supply(config, {"this": "must not be consumed during export"})
    first = export_upgrade_inventory(config, tmp_path)
    second = export_upgrade_inventory(config, tmp_path)
    assert config.read_bytes() == before
    assert first["binding"] == second["binding"]
    assert first["state_sha256"] == second["state_sha256"]
    assert first["orders"][0]["legacy_delivery_id"] == "old-delivery"
    assert first["orders"][0]["legacy_status"] == "EM_ANDAMENTO"
    assert first["couriers"] == [{"local_id": "5", "legacy_id": "old-courier"}]
    assert rows(database, "SELECT external_delivery_id FROM DeliveryMapping") == [("old-delivery",)]


def test_export_includes_business_and_raw_source_context_but_never_credentials_or_token_bodies(tmp_path):
    config, database, _, document = installed(tmp_path, mappings=False)
    document["farmax"]["password"] = "private-source-password"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    config.with_name("token.txt").write_text("private-stored-token", encoding="utf-8")
    adopted = upgrade_installation(config, tmp_path)
    journal = SyncJournal(str(database), configured_scope(adopted.settings))
    journal.accept("42", {"internal_id": "42", "customerName": "Business evidence", "source_context": {
        "branch_id": "7", "original_sale": {"id": "42", "password": "private-source-password"},
        "access_token": "private-access-token", "opaque": "private-source-password"}})
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET request=?,source_receipt_raw='2026-11-12T12:00:00'", (json.dumps({
            "query": "opaque GraphQL private-query-token", "headers": {"Authorization": "Bearer private-header-token"},
            "variables": {"input": {"rawAddress": "Original address", "createdAtIntent": "2026-11-12T12:00:00",
                "sourceContext": {"branch_id": "7", "refreshToken": "private-refresh-token"}}}}),))
        conn.execute("INSERT INTO SyncSource(scope,source_id,payload) VALUES(?,?,?)", (configured_scope(adopted.settings),
            "observed", json.dumps({"raw_source": {"sale": "42", "password": "private-source-password", "token": "private-source-token"}})))
    exported = export_upgrade_inventory(config, tmp_path)
    serialized = json.dumps(exported)
    assert "private-" not in serialized
    assert "Business evidence" in serialized and "Original address" in serialized
    assert exported["orders"][0]["payload"]["source_context"]["original_sale"]["id"] == "42"
    assert exported["source_observations"][0]["source_context"] == {"raw_source": {"sale": "42"}}
    assert exported["orders"][0]["has_request"]
    assert "auth" not in exported and "farmax" not in exported


def test_regenerated_inventory_includes_mapping_written_by_original_legacy_release_after_first_export(tmp_path):
    config, database, _, _ = installed(tmp_path)
    initial = export_upgrade_inventory(config, tmp_path)
    with sqlite3.connect(str(database)) as conn:
        conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('old-newly-retained','456','ADICIONADO')")
    regenerated = export_upgrade_inventory(config, tmp_path)
    assert {item["internal_id"] for item in regenerated["orders"]} == {"123", "456"}
    assert regenerated["binding"] == initial["binding"]
    assert regenerated["state_sha256"] != initial["state_sha256"]
    added = next(item for item in regenerated["orders"] if item["internal_id"] == "456")
    assert added["legacy_delivery_id"] == "old-newly-retained" and added["submission"] == "IMPORTED"


def test_export_cds_metadata_retains_unknown_and_unreadable_receipts_without_exporting_raw_blobs(tmp_path):
    config, _, folder, _ = installed(tmp_path, "CDS", mappings=False)
    path, internal = receipt(folder)
    raw = json.loads(path.read_text())
    raw["password"] = "private-cds-password"
    path.write_text(json.dumps(raw), encoding="windows-1252")
    (folder / "ent-partial.json").write_bytes(b'{"token":"private-partial-token",')
    exported = export_upgrade_inventory(config, tmp_path)
    assert "private-" not in json.dumps(exported)
    assert len(exported["cds_files"]) == 2
    parsed = next(item for item in exported["cds_files"] if item["internal_id"])
    incomplete = next(item for item in exported["cds_files"] if not item["internal_id"])
    assert parsed["internal_id"] == internal and parsed["resolution"] is None
    assert incomplete["parse_error"] and incomplete["receipt"] is None
    assert exported["orders"][0]["source_review_kind"] == "CDS_UPGRADE_UNRESOLVED"


def test_changed_work_snapshot_rejects_generated_package_without_config_or_mapping_loss(tmp_path):
    config, database, _, _ = installed(tmp_path)
    inventory = export_upgrade_inventory(config, tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["inventory_state_sha256"] = inventory["state_sha256"]
    supply(config, package)
    before = config.read_bytes()
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET attempts=attempts+1")
    result = upgrade_installation(config, tmp_path)
    assert "changed after inventory export" in result.package_error
    assert config.read_bytes() == before
    assert rows(database, "SELECT external_delivery_id FROM DeliveryMapping") == [("old-delivery",)]


def test_matching_export_snapshot_allows_native_cutover_and_repeat_ignores_post_cutover_changes(tmp_path):
    config, database, _, _ = installed(tmp_path)
    inventory = export_upgrade_inventory(config, tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["inventory_state_sha256"] = inventory["state_sha256"]
    supply(config, package)
    applied = upgrade_installation(config, tmp_path)
    assert applied.native_applied, applied.package_error
    with sqlite3.connect(applied.database_path) as conn:
        conn.execute("UPDATE SyncOrders SET attempts=attempts+1")
    assert not upgrade_installation(config, tmp_path).package_error


def test_inventory_cli_uses_selected_config_and_refuses_to_overwrite_existing_files(tmp_path):
    config, _, _, _ = installed(tmp_path)
    output = tmp_path / "installation-inventory.json"
    source = Path(__file__).resolve().parents[2] / "src" / "native_migration.py"
    executable = os.environ.get("VELIDE_UPGRADE_EXE")
    command = [executable] if executable else [sys.executable, str(source)]
    command += ["--export-upgrade-inventory", "--config", str(config),
                "--bundle-dir", str(tmp_path), "--output", str(output)]
    env = dict(os.environ, VELIDE_CONFIG_PATH=str(tmp_path / "unavailable.yml"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["kind"] == "velide-upgrade-inventory" and exported["orders"]
    original = output.read_bytes()
    repeated = subprocess.run(command, env=env, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert output.read_bytes() == original


def test_inventory_refuses_to_disclose_endpoint_embedded_credentials(tmp_path):
    config, _, _, document = installed(tmp_path)
    document["api"]["velide_server"] = "https://private-user:private-password@legacy.invalid/"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="endpoint containing credentials"):
        export_upgrade_inventory(config, tmp_path)
