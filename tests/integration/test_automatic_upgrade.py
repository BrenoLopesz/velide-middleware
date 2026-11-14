"""Upgrade regressions using original-style mappings, real files and real SQLite."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from api.sync_journal import SyncJournal
from config import Settings, install_effective_settings
import local_upgrade
from local_upgrade import adopt_observed_farmax_branch, installation_binding, upgrade_installation
from upgrade_cds import cds_upgrade_hold
from utils.source_identity import cds_file_identity, configured_scope, configured_source_identity


NATIVE = {
    "merchant_id": "10000000-0000-0000-0000-000000000001",
    "connection_id": "10000000-0000-0000-0000-000000000002",
    "timezone": "America/Sao_Paulo",
}
DELIVERY = "10000000-0000-0000-0000-000000000003"
COURIER = "10000000-0000-0000-0000-000000000004"


def installed(tmp_path, target="Farmax", mappings=True):
    resources = tmp_path / "resources"
    resources.mkdir()
    folder = tmp_path / "cds"
    folder.mkdir()
    document = {
        "auth": {"domain": "existing.invalid", "client_id": "installed-client",
                 "scope": "openid offline_access", "audience": "installed-audience"},
        "api": {"velide_server": "https://legacy.invalid/", "velide_websockets_server": "wss://legacy.invalid/",
                "use_neighbourhood": True, "use_ssl": False, "timeout": 29,
                "add_delivery_max_retries": 7, "add_delivery_initial_delay": 8,
                "add_delivery_backoff_factor": 3, "add_delivery_idempotency_enabled": False,
                "future_preference": "retain"},
        "farmax": {"host": "localhost", "file": "existing.fdb", "user": "local", "password": "test-only"},
        "folder_to_watch": str(folder), "target_system": target,
        "sqlite_path": "resources/velide.db", "sqlite_days_retention": 91,
        "reconciliation": {"retry_reconciliation_delay_seconds": 8, "sync_interval_ms": 2000},
        "future_setting": {"retain": True},
    }
    config = resources / "config.yml"
    config.write_text("# Existing installation; keep exactly on ordinary updates\n" + yaml.safe_dump(document), encoding="utf-8")
    database = resources / "velide.db"
    with sqlite3.connect(str(database)) as conn:
        conn.execute("""CREATE TABLE DeliveryMapping (
            external_delivery_id TEXT PRIMARY KEY NOT NULL, internal_delivery_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL, deliveryman_id TEXT, create_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("CREATE TABLE DeliverymenMapping(velide_id TEXT PRIMARY KEY,local_id TEXT UNIQUE)")
        if mappings:
            conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status,deliveryman_id) VALUES('old-delivery','123','EM_ANDAMENTO','5')")
            conn.execute("INSERT INTO DeliverymenMapping VALUES('old-courier','5')")
    return config, database, folder, document


def rows(database, sql, args=()):
    conn = sqlite3.connect(str(database))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def package_for(config, database, result):
    new_scope = "https://native.invalid/graphql|" + result.settings.target_system.value + "|" + NATIVE["merchant_id"] + "|" + NATIVE["connection_id"]
    package = {
        "version": 1, "authority": {"export_id": "server-migration-export-fixture"},
        "binding": installation_binding(config, database),
        "configuration": {"api": {"velide_server": "https://native.invalid/graphql",
                                   "velide_websockets_server": "wss://native.invalid/graphql", "native": NATIVE}},
        "manifest": {"version": 1, "old_scope": configured_scope(result.settings), "new_scope": new_scope,
                     "deliveries": [], "couriers": []},
    }
    for internal, external, state in rows(database, "SELECT internal_id,external_id,submission FROM SyncOrders"):
        package["manifest"]["deliveries"].append({"internal_id": internal, "legacy_delivery_id": external,
            "native_delivery_id": DELIVERY if external else None,
            "state": "IMPORTED" if external else state})
    for local, old in rows(database, "SELECT local_id,velide_id FROM DeliverymenMapping"):
        package["manifest"]["couriers"].append({"local_id": local, "legacy_id": old, "native_id": COURIER})
    return package


def supply(config, package):
    config.with_name("native-upgrade.json").write_text(json.dumps(package), encoding="utf-8")


def receipt(folder, name="ent1.json", timestamp="2026-10-01T12:34:56"):
    value = {"nome_cliente": "Cliente", "endereco": "Rua 1", "horario_pedido": timestamp,
             "contato_cliente": "1234", "valor_pedido": "12,50"}
    path = folder / name
    path.write_bytes(json.dumps(value).encode("windows-1252"))
    return path, cds_file_identity(name, datetime.fromisoformat(timestamp))


def test_original_update_automatically_adopts_keeps_config_and_exact_mappings(tmp_path):
    config, database, _, _ = installed(tmp_path)
    raw = config.read_bytes()
    mappings = rows(database, "SELECT * FROM DeliveryMapping")
    result = upgrade_installation(config, tmp_path)
    assert result.adopted and not result.native_applied and not result.package_error
    assert config.read_bytes() == raw
    assert rows(database, "SELECT * FROM DeliveryMapping") == mappings
    assert rows(database, "SELECT * FROM DeliverymenMapping") == [("old-courier", "5")]
    journal = SyncJournal(str(database), configured_scope(result.settings), configured_source_identity(result.settings))
    assert journal.restore()[0]["external_id"] == "old-delivery"
    assert rows(result.backups[0], "PRAGMA user_version") == [(0,)]
    repeated = upgrade_installation(config, tmp_path)
    assert not repeated.adopted and repeated.installation_id == result.installation_id
    assert not repeated.backups


def test_branch_is_adopted_from_live_observation_without_manifest_and_conflicts_fail(tmp_path):
    config, database, _, _ = installed(tmp_path)
    result = upgrade_installation(config, tmp_path)
    with sqlite3.connect(str(database)) as conn:
        assert adopt_observed_farmax_branch(conn, configured_scope(result.settings), "07")
    assert rows(database, "SELECT farmax_branch FROM SyncScope") == [("7",)]
    with sqlite3.connect(str(database)) as conn, pytest.raises(ValueError, match="conflicts"):
        adopt_observed_farmax_branch(conn, configured_scope(result.settings), "8")


def test_branch_explicit_provenance_is_not_relabelled(tmp_path):
    config, database, _, _ = installed(tmp_path)
    result = upgrade_installation(config, tmp_path)
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET payload=?", (json.dumps({"source_context": {"branch_id": "9"}}),))
    with sqlite3.connect(str(database)) as conn, pytest.raises(ValueError, match="another Farmax branch"):
        adopt_observed_farmax_branch(conn, configured_scope(result.settings), "7")


def test_pending_request_and_cancellation_survive_adoption(tmp_path):
    config, database, _, document = installed(tmp_path, mappings=False)
    journal = SyncJournal(str(database), configured_scope(Settings.model_validate(document)))
    journal.accept("pending", {"internal_id": "pending"})
    journal.freeze_request("pending", {"original": "request"})
    journal.claim()
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET cancellation='REQUESTED'")
    before = rows(database, "SELECT * FROM SyncOrders")
    upgrade_installation(config, tmp_path)
    assert rows(database, "SELECT * FROM SyncOrders") == before


def test_changed_source_is_rejected_without_rewriting_data(tmp_path):
    config, database, _, document = installed(tmp_path)
    upgrade_installation(config, tmp_path)
    before = rows(database, "SELECT * FROM SyncScope")
    document["farmax"]["file"] = "other.fdb"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="binding changed"):
        upgrade_installation(config, tmp_path)
    assert rows(database, "SELECT * FROM SyncScope") == before


def test_unbound_uuid_shaped_mappings_do_not_authorize_native_adoption(tmp_path):
    config, database, _, document = installed(tmp_path)
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE DeliveryMapping SET external_delivery_id=?", (DELIVERY,))
        conn.execute("UPDATE DeliverymenMapping SET velide_id=?", (COURIER,))
    document["api"].update(native=NATIVE, velide_server="https://native.invalid/graphql")
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="authoritative"):
        upgrade_installation(config, tmp_path)
    assert rows(database, "PRAGMA user_version") == [(0,)]


def test_cds_original_files_are_retained_as_unknown_not_terminal_or_dispatched(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS")
    path, internal = receipt(folder)
    original = path.read_bytes()
    result = upgrade_installation(config, tmp_path)
    assert result.unresolved_cds == 1 and result.instructions
    assert json.loads(Path(result.cds_baseline_path).read_text())["unobserved_files"] == {}
    assert path.read_bytes() == original
    assert rows(database, "SELECT original,resolution FROM UpgradeCdsFiles") == [(original, None)]
    journal = SyncJournal(str(database), configured_scope(result.settings))
    row = journal.accept(internal, {"internal_id": internal})
    assert row["submission"] == "NEEDS_REVIEW" and row["source_review"]
    assert journal.claim() is None
    _, new_id = receipt(folder, "ent2.json")
    repeated = upgrade_installation(config, tmp_path)
    assert repeated.unresolved_cds == 1  # Never re-baseline new files on restart.
    with sqlite3.connect(str(database)) as conn:
        assert cds_upgrade_hold(conn, configured_scope(result.settings), internal)
        assert not cds_upgrade_hold(conn, configured_scope(result.settings), new_id, "ent2.json")
    journal.accept(new_id, {"internal_id": new_id})
    assert journal.claim()["internal_id"] == new_id


def test_incomplete_original_cds_receipt_has_durable_filename_guard(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS", mappings=False)
    (folder / "ent1.json").write_bytes(b'{"incomplete":')
    result = upgrade_installation(config, tmp_path)
    assert result.unresolved_cds == 1
    with sqlite3.connect(str(database)) as conn:
        assert cds_upgrade_hold(conn, configured_scope(result.settings), "later-completed-id", "ent1.json")
        assert cds_upgrade_hold(conn, configured_scope(result.settings), "missing-filename")
        assert not cds_upgrade_hold(conn, configured_scope(result.settings), "new", "ent2.json")


def test_inventory_failure_publishes_no_partial_adoption(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS")
    folder.rmdir()
    raw = config.read_bytes()
    with pytest.raises(OSError):
        upgrade_installation(config, tmp_path)
    assert rows(database, "PRAGMA user_version") == [(0,)]
    assert not rows(database, "SELECT name FROM sqlite_master WHERE name='UpgradeInstallation'")
    assert config.read_bytes() == raw


def test_native_package_automatically_maps_active_route_preserves_preferences_and_repeats(tmp_path):
    config, database, _, original = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    supply(config, package)
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied and not result.package_error
    assert Path(result.database_path) != database
    assert rows(database, "SELECT external_delivery_id,status FROM DeliveryMapping") == [("old-delivery", "EM_ANDAMENTO")]
    assert rows(result.database_path, "SELECT external_delivery_id,status FROM DeliveryMapping") == [(DELIVERY, "EM_ANDAMENTO")]
    assert rows(result.database_path, "SELECT velide_id FROM DeliverymenMapping") == [(COURIER,)]
    after = yaml.safe_load(config.read_text(encoding="utf-8"))
    for key, value in original.items():
        if key not in ("api", "sqlite_path"):
            assert after[key] == value
    for key, value in original["api"].items():
        if key not in ("velide_server", "velide_websockets_server"):
            assert after["api"][key] == value
    assert result.settings.api.use_neighbourhood
    assert result.settings.api.add_delivery_max_retries == 7
    held_config_reference = adopted.settings
    assert install_effective_settings(held_config_reference, result.settings) is held_config_reference
    assert held_config_reference.api.native.connection_id == NATIVE["connection_id"]
    assert held_config_reference.sqlite_path == result.database_path
    assert not upgrade_installation(config, tmp_path).package_error
    with sqlite3.connect(result.database_path) as conn:
        assert adopt_observed_farmax_branch(conn, configured_scope(result.settings), "7")


@pytest.mark.parametrize("corruption", ["installation", "mapping", "config", "retry", "auth", "scope"])
def test_bad_package_leaves_legacy_config_and_data_usable(tmp_path, corruption):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    if corruption == "installation":
        package["binding"]["installation_id"] = "another-store"
    elif corruption == "mapping":
        package["manifest"]["deliveries"][0]["legacy_delivery_id"] = "guessed-id"
    elif corruption == "config":
        package["binding"]["config_sha256"] = "stale"
    elif corruption == "retry":
        package["configuration"]["api"]["add_delivery_max_retries"] = 1
    elif corruption == "auth":
        package["configuration"]["auth"] = {"domain": "guessed.invalid"}
    else:
        package["manifest"]["new_scope"] += "wrong"
    supply(config, package)
    before = config.read_bytes()
    mappings = rows(database, "SELECT * FROM DeliveryMapping")
    result = upgrade_installation(config, tmp_path)
    assert result.package_error and result.instructions and not result.native_applied
    assert result.settings.api.native is None
    assert config.read_bytes() == before
    assert rows(database, "SELECT * FROM DeliveryMapping") == mappings


def test_failed_config_publication_is_repeatable_and_old_database_stays_active(tmp_path, monkeypatch):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    supply(config, package_for(config, database, adopted))
    before = config.read_bytes()
    real_replace = local_upgrade.os.replace
    def fail(*args):
        raise OSError("simulated publication interruption")
    monkeypatch.setattr(local_upgrade.os, "replace", fail)
    result = upgrade_installation(config, tmp_path)
    assert "interruption" in result.package_error
    assert config.read_bytes() == before
    assert rows(database, "SELECT external_delivery_id FROM DeliveryMapping") == [("old-delivery",)]
    assert not list(database.parent.glob("velide.native-*.db"))
    monkeypatch.setattr(local_upgrade.os, "replace", real_replace)
    assert upgrade_installation(config, tmp_path).native_applied


def test_package_cannot_requeue_previously_attempted_legacy_work(tmp_path):
    config, database, _, document = installed(tmp_path, mappings=False)
    journal = SyncJournal(str(database), configured_scope(Settings.model_validate(document)))
    journal.accept("uncertain", {"internal_id": "uncertain"})
    journal.freeze_request("uncertain", {"idempotencyKey": "original-key"})
    journal.claim()
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["manifest"]["deliveries"][0]["state"] = "QUEUED"
    supply(config, package)
    before = rows(database, "SELECT * FROM SyncOrders")
    result = upgrade_installation(config, tmp_path)
    assert "never-submitted" in result.package_error
    assert rows(database, "SELECT * FROM SyncOrders") == before


def test_cds_package_cannot_silently_mark_unknown_original_file_processed(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS", mappings=False)
    receipt(folder)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["manifest"]["deliveries"][0]["state"] = "TERMINAL"
    supply(config, package)
    result = upgrade_installation(config, tmp_path)
    assert "remain held" in result.package_error
    assert result.unresolved_cds == 1


def test_cds_authoritative_byte_bound_resolution_is_consumed_automatically(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS", mappings=False)
    path, internal = receipt(folder)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["manifest"]["deliveries"][0]["state"] = "TERMINAL"
    package["cds_resolutions"] = [{"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "outcome": "PROCESSED", "evidence": {"authoritative_receipt": "export-receipt-1"}}]
    supply(config, package)
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied and result.unresolved_cds == 0, result.package_error
    assert rows(result.database_path, "SELECT submission FROM SyncOrders WHERE internal_id=?", (internal,)) == [("TERMINAL",)]
    assert path.exists()


def test_wal_contents_are_included_in_adoption_backup(tmp_path):
    config, database, _, _ = installed(tmp_path)
    writer = sqlite3.connect(str(database))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('wal-delivery','456','ADICIONADO')")
        writer.commit()
        result = upgrade_installation(config, tmp_path)
        assert rows(result.backups[0], "SELECT external_delivery_id FROM DeliveryMapping WHERE internal_delivery_id='456'") == [("wal-delivery",)]
        assert rows(database, "SELECT external_id FROM SyncOrders WHERE internal_id='456'") == [("wal-delivery",)]
    finally:
        writer.close()


def test_cds_link_to_original_random_id_does_not_duplicate_or_relabel_delivery(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS")
    path, internal = receipt(folder)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    receipt_entry = next(item for item in package["manifest"]["deliveries"] if item["internal_id"] == internal)
    receipt_entry["state"] = "TERMINAL"
    package["cds_resolutions"] = [{"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "outcome": "LINKED", "retained_internal_id": "123", "evidence": {"server_receipt": "exact-original-export"}}]
    supply(config, package)
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied and result.unresolved_cds == 0, result.package_error
    assert rows(result.database_path, "SELECT external_delivery_id,internal_delivery_id FROM DeliveryMapping") == [(DELIVERY, "123")]
    assert rows(result.database_path, "SELECT submission FROM SyncOrders WHERE internal_id=?", (internal,)) == [("TERMINAL",)]


def test_native_cutover_defers_pending_effect_without_losing_obligation(tmp_path):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    with sqlite3.connect(str(database)) as conn:
        conn.execute("INSERT INTO SyncEffects(scope,internal_id,effect_key,operation,payload) VALUES(?,'123','pending','DEPARTURE','{}')", (configured_scope(adopted.settings),))
    supply(config, package_for(config, database, adopted))
    before = rows(database, "SELECT * FROM SyncEffects")
    result = upgrade_installation(config, tmp_path)
    assert "outstanding" in result.package_error
    assert rows(database, "SELECT * FROM SyncEffects") == before


def test_native_copy_keeps_audit_of_original_frozen_request(tmp_path):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    with sqlite3.connect(str(database)) as conn:
        conn.execute("UPDATE SyncOrders SET request=?,attempts=3,remote_revision='999',remote_state='{}'", (json.dumps({"idempotencyKey": "original"}),))
    supply(config, package_for(config, database, adopted))
    result = upgrade_installation(config, tmp_path)
    assert result.native_applied, result.package_error
    audit = json.loads(rows(result.database_path, "SELECT original FROM SyncMigrationOrders")[0][0])
    assert json.loads(audit["request"])["idempotencyKey"] == "original" and audit["attempts"] == 3
    assert audit["remote_revision"] == "999"
    assert rows(result.database_path, "SELECT remote_revision,remote_state FROM SyncOrders") == [(None, None)]


def test_pre_v3_pending_effect_is_not_silently_superseded_by_schema_adoption(tmp_path):
    config, database, _, document = installed(tmp_path)
    scope = configured_scope(Settings.model_validate(document))
    # Genuine v2 layout: the current schema migrator normally supersedes pending
    # effects while adding v3 columns. Automatic adoption must retain them.
    with sqlite3.connect(str(database)) as conn:
        conn.executescript("""
            CREATE TABLE SyncOrders(scope TEXT,internal_id TEXT,payload TEXT,request TEXT,
                submission TEXT DEFAULT 'QUEUED',cancellation TEXT DEFAULT 'NONE',external_id TEXT,
                job_id TEXT,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,last_error TEXT,remote_state TEXT,
                PRIMARY KEY(scope,internal_id));
            CREATE TABLE SyncEffects(scope TEXT,internal_id TEXT,effect_key TEXT,operation TEXT,payload TEXT,
                state TEXT DEFAULT 'PENDING',attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,last_error TEXT,
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,UNIQUE(scope,internal_id,effect_key));
            CREATE TABLE SyncSource(scope TEXT,source_id TEXT,payload TEXT,resolved INTEGER DEFAULT 0,last_error TEXT,
                PRIMARY KEY(scope,source_id));
            CREATE TABLE SyncCheckpoints(scope TEXT PRIMARY KEY,cursor INTEGER);
            CREATE TABLE SyncScope(id INTEGER PRIMARY KEY,scope TEXT);
            PRAGMA user_version=2;
        """)
        conn.execute("INSERT INTO SyncScope VALUES(1,?)", (scope,))
        conn.execute("INSERT INTO SyncEffects(scope,internal_id,effect_key,operation,payload) VALUES(?,'123','retained','DEPARTURE','{}')", (scope,))
    upgrade_installation(config, tmp_path)
    assert rows(database, "SELECT state,operation,payload FROM SyncEffects") == [("PENDING", "DEPARTURE", "{}")]


def test_automatic_cli_uses_selected_config_without_importing_workstation_defaults(tmp_path):
    import os
    import subprocess
    import sys
    config, database, _, _ = installed(tmp_path)
    env = dict(os.environ, VELIDE_CONFIG_PATH=str(tmp_path / "does-not-exist.yml"), PYTHONDONTWRITEBYTECODE="1")
    executable = os.environ.get("VELIDE_UPGRADE_EXE")
    command = [executable] if executable else [sys.executable, str(Path(__file__).resolve().parents[2] / "src" / "native_migration.py")]
    command += ["--automatic", "--config", str(config), "--bundle-dir", str(tmp_path)]
    process = subprocess.run(command, env=env, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["adopted"] and not report["native_applied"]
    assert rows(database, "SELECT external_delivery_id FROM DeliveryMapping") == [("old-delivery",)]
    adopted = upgrade_installation(config, tmp_path)
    supply(config, package_for(config, database, adopted))
    cutover = subprocess.run(command, env=env, capture_output=True, text=True)
    assert cutover.returncode == 0, cutover.stderr
    assert json.loads(cutover.stdout)["native_applied"], cutover.stdout
    repeated = subprocess.run(command, env=env, capture_output=True, text=True)
    assert repeated.returncode == 0, repeated.stderr
    assert not json.loads(repeated.stdout)["package_error"]
