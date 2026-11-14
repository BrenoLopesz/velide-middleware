"""Automatic installation adoption and optional, installation-bound native cutover.

Call upgrade_installation after acquiring the application instance lock, before
opening services/ERP connections. The CLI takes that same lock. No network calls
or source-database writes occur here. A trusted provisioning channel may place
native-upgrade.json next to config.yml; ordinary updates need no package.

Native publication is copy-on-write: build/validate a complete new SQLite file,
then atomically replace ONLY the config pointer/endpoints. Interrupted attempts
leave unused DB copies; they never require guessing whether two live files agree.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

import yaml
from filelock import FileLock

from upgrade_cds import create_inventory_table, inventory_cds, resolve_cds_inventory
from upgrade_state import scoped_upgrade_tables
from utils.source_identity import (
    configured_scope, configured_source_identity, configured_source_timezone,
)


@dataclass
class UpgradeResult:
    settings: object
    database_path: str
    installation_id: str
    adopted: bool = False
    native_applied: bool = False
    package_error: str = None
    unresolved_cds: int = 0
    cds_baseline_path: str = None
    backups: list = field(default_factory=list)
    instructions: list = field(default_factory=list)
    token_path: str = None


def _connect(path, readonly=False):
    return sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)


def _has_table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _copy_database(source, destination):
    reader = _connect(source, readonly=True)
    try:
        target = sqlite3.connect(str(destination))
        try:
            reader.backup(target)
        finally:
            target.close()
    finally:
        reader.close()


def _new_path(parent, prefix, suffix):
    fd, path = tempfile.mkstemp(dir=str(parent), prefix=prefix, suffix=suffix)
    os.close(fd)
    return Path(path)


def _write_bytes(path, contents):
    with open(str(path), "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _binding(conn, settings):
    scope = configured_scope(settings)
    identity = configured_source_identity(settings)
    zone = configured_source_timezone(settings)
    if identity is None:
        raise ValueError("Installed source configuration is incomplete")
    row = None
    if _has_table(conn, "SyncScope"):
        columns = {item[1] for item in conn.execute("PRAGMA table_info(SyncScope)")}
        fields = [name if name in columns else "NULL" for name in ("scope", "source_identity", "source_timezone")]
        row = conn.execute("SELECT " + ",".join(fields) + " FROM SyncScope WHERE id=1").fetchone()
    if row and (row[0] != scope or row[1] not in (None, identity)
                or (zone is not None and row[2] not in (None, zone))):
        raise ValueError("Installed endpoint/source binding changed; existing data was preserved")
    if row and zone is None:
        zone = row[2]  # Preserve an already bound zone missing from older YAML.
    if settings.api.native and (not row or row[1] is None):
        # UUID-shaped legacy IDs are still legacy IDs. Shape is not ownership.
        for table in ("DeliveryMapping", "DeliverymenMapping", "SyncOrders"):
            if _has_table(conn, table) and conn.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone():
                raise ValueError("Existing unbound mappings require authoritative native cutover")
    for table in scoped_upgrade_tables(conn):
        if _has_table(conn, table) and conn.execute(
            "SELECT 1 FROM " + table + " WHERE scope<>? LIMIT 1", (scope,)
        ).fetchone():
            raise ValueError("Retained work belongs to another endpoint/source")
    return scope, identity, zone


def _adopt(database, settings, result):
    """Migrate on a working copy, then publish with SQLite's atomic backup API."""
    exists = database.exists()
    pending_effects = []
    if exists:
        conn = _connect(database, readonly=True)
        try:
            _binding(conn, settings)
            if _has_table(conn, "UpgradeInstallation"):
                row = conn.execute("SELECT installation_id FROM UpgradeInstallation WHERE id=1").fetchone()
                if row:
                    return row[0]
            if _has_table(conn, "SyncEffects"):
                pending_effects = [row[0] for row in conn.execute("SELECT sequence FROM SyncEffects WHERE state='PENDING'")]
        finally:
            conn.close()
        backup = _new_path(database.parent, database.name + ".pre-upgrade-", ".db")
        _copy_database(database, backup)
        result.backups.append(str(backup))
    staged = _new_path(database.parent, database.name + ".adopt-", ".db")
    try:
        if exists:
            _copy_database(database, staged)
        # Import lazily: the executable can select --config before config loads.
        from api.sqlite_manager import SQLiteManager
        from api.sync_journal import SyncJournal

        with SQLiteManager(str(staged)) as db:
            conn = db._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            scope, identity, zone = _binding(conn, settings)
            for sequence in pending_effects:
                # The older schema upgrader supersedes effects. Adoption is not
                # an acknowledgement: retain every outstanding obligation.
                conn.execute("UPDATE SyncEffects SET state='PENDING' WHERE sequence=?", (sequence,))
            conn.execute("INSERT OR IGNORE INTO SyncScope(id,scope) VALUES(1,?)", (scope,))
            conn.execute("UPDATE SyncScope SET source_identity=?,source_timezone=? WHERE id=1", (identity, zone))
            conn.execute("""CREATE TABLE UpgradeInstallation (
                id INTEGER PRIMARY KEY CHECK(id=1), installation_id TEXT NOT NULL,
                scope TEXT NOT NULL, source_identity TEXT NOT NULL,
                legacy_adopted INTEGER NOT NULL, adopted_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            installation_id = str(uuid.uuid4())  # Local installation ID, NEVER a native API ID.
            conn.execute("INSERT INTO UpgradeInstallation(id,installation_id,scope,source_identity,legacy_adopted) VALUES(1,?,?,?,?)",
                         (installation_id, scope, identity, int(not settings.api.native)))
            create_inventory_table(conn)
            if settings.target_system.value == "CDS" and not settings.api.native:
                inventory_cds(conn, scope, settings.folder_to_watch)
        SyncJournal(str(staged), configured_scope(settings), configured_source_identity(settings),
                    configured_source_timezone(settings)).restore()
        conn = _connect(staged, readonly=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Adopted database integrity check failed")
        finally:
            conn.close()
        # SQLite backup commits the replacement atomically, including WAL data.
        # Caller holds the app instance lock and has not opened any services.
        _copy_database(staged, database)
        result.adopted = True
        return installation_id
    finally:
        # Includes the old schema migrator's backup of our private working copy.
        for path in staged.parent.glob(staged.name + "*"):
            path.unlink()


def adopt_observed_farmax_branch(conn, scope, branch):
    """SyncJournal.bind_farmax_branch hook, within its existing write transaction.

    Call before its old missing-provenance rejection. Return True if handled.
    `branch` must come from the connected source's normal branch query, never
    from the installer. Missing historical context is compatible; a conflicting
    explicit context or already bound branch is not. No ERP qualification gate.
    """
    if not _has_table(conn, "UpgradeInstallation"):
        return False
    adopted = conn.execute("SELECT scope,legacy_adopted FROM UpgradeInstallation WHERE id=1").fetchone()
    if not adopted or adopted != (scope, 1) or scope.split("|")[1] != "Farmax":
        return False
    from connectors.farmax.identity import normalize_farmax_id
    branch = normalize_farmax_id(branch)
    previous = conn.execute("SELECT farmax_branch FROM SyncScope WHERE id=1").fetchone()[0]
    if previous not in (None, branch):
        raise ValueError("Observed Farmax branch conflicts with installed source")
    for (payload,) in conn.execute("SELECT payload FROM SyncOrders WHERE scope=?", (scope,)):
        context = (json.loads(payload or "{}") or {}).get("source_context") or {}
        old = context.get("branch_id")
        if old is not None and normalize_farmax_id(old) != branch:
            raise ValueError("Retained work explicitly belongs to another Farmax branch")
    conn.execute("UPDATE SyncScope SET farmax_branch=? WHERE id=1", (branch,))
    return True


def installation_binding(config_path, database_path):
    """Provisioning export: exact local binding; no credentials or invented API IDs."""
    conn = _connect(database_path, readonly=True)
    try:
        row = conn.execute("SELECT installation_id,scope,source_identity FROM UpgradeInstallation WHERE id=1").fetchone()
        return {"installation_id": row[0], "old_scope": row[1], "source_identity": row[2],
                "config_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest()}
    finally:
        conn.close()


def _native_cutover(config_path, raw, document, database, package, result):
    if package.get("version") != 1 or not package.get("authority"):
        raise ValueError("An authoritative version-1 installation package is required")
    identity = hashlib.sha256(json.dumps(package, sort_keys=True).encode("utf-8")).hexdigest()
    conn = _connect(database, readonly=True)
    try:
        if _has_table(conn, "UpgradePackages") and conn.execute(
            "SELECT 1 FROM UpgradePackages WHERE identity=?", (identity,)
        ).fetchone():
            return  # Publication completed on a previous run.
    finally:
        conn.close()
    if package.get("binding") != installation_binding(config_path, database):
        raise ValueError("Migration package does not match this installation/configuration")
    if "inventory_state_sha256" in package:
        from upgrade_inventory import inventory_state_fingerprint
        conn = _connect(database, readonly=True)
        try:
            if inventory_state_fingerprint(conn) != package["inventory_state_sha256"]:
                raise ValueError("Retained work changed after inventory export; regenerate the server upgrade package")
        finally:
            conn.close()
    if result.settings.api.native:
        raise ValueError("Native-to-native scope changes need a separate migration")
    manifest = package["manifest"]
    # The package may NOT smuggle baseline declarations bypassing the byte-bound inventory.
    if manifest.get("processed_cds_files"):
        raise ValueError("Automatic packages must resolve inventoried CDS files by sha256")
    staged = _new_path(database.parent, database.stem + ".native-" + identity[:12] + "-", ".db")
    try:
        _copy_database(database, staged)
        _publish_native_copy(config_path, raw, document, package, result, identity, staged)
    except BaseException:
        # Ordinary rejected packages should not accumulate full database copies
        # on every launch. If publication might have happened, keep the copy.
        if config_path.read_bytes() == raw:
            for path in staged.parent.glob(staged.name + "*"):
                path.unlink()
            result.backups = [path for path in result.backups if Path(path).exists()]
        raise


def _publish_native_copy(config_path, raw, document, package, result, identity, staged):
    from config import Settings, native_upgrade_document
    from native_migration import migrate

    manifest = package["manifest"]
    proposed = native_upgrade_document(document, package["configuration"], staged)
    new_settings = Settings.model_validate(proposed)
    if manifest["old_scope"] != configured_scope(result.settings) or manifest["new_scope"] != configured_scope(new_settings):
        raise ValueError("Manifest scopes disagree with installed/proposed configuration")
    if configured_source_identity(new_settings) != configured_source_identity(result.settings):
        raise ValueError("Cutover cannot change the source")
    old_zone = configured_source_timezone(result.settings)
    if old_zone and old_zone != configured_source_timezone(new_settings):
        raise ValueError("Cutover cannot reinterpret bound source timestamps")
    conn = _connect(staged)
    try:
        conn.execute("BEGIN IMMEDIATE")
        bound_zone = conn.execute("SELECT source_timezone FROM SyncScope WHERE id=1").fetchone()[0]
        if bound_zone and bound_zone != configured_source_timezone(new_settings):
            raise ValueError("Cutover cannot reinterpret bound source timestamps")
        resolutions = package.get("cds_resolutions", [])
        resolve_cds_inventory(conn, manifest["old_scope"], resolutions)
        for entry in resolutions:
            internal = conn.execute("SELECT internal_id FROM UpgradeCdsFiles WHERE scope=? AND filename=?",
                (manifest["old_scope"], entry["filename"].lower())).fetchone()[0]
            if entry["outcome"] in ("PROCESSED", "NOT_SUBMITTED"):
                expected = "TERMINAL" if entry["outcome"] == "PROCESSED" else "QUEUED"
                matches = [item for item in manifest["deliveries"] if item["internal_id"] == internal]
                if len(matches) != 1 or matches[0]["state"] != expected or matches[0].get("native_delivery_id") or matches[0].get("native_job_id"):
                    raise ValueError("CDS receipt outcome disagrees with its manifest outcome")
            if entry["outcome"] == "LINKED":
                retained = entry.get("retained_internal_id", internal)
                matches = [item for item in manifest["deliveries"] if item["internal_id"] == retained]
                if len(matches) != 1 or not matches[0].get("native_delivery_id") or matches[0]["state"] != "IMPORTED":
                    raise ValueError("Linked CDS receipt requires an exact native delivery mapping")
                if retained != internal:
                    # Explicit link to an original random ID: keep that active
                    # mapping; retain the receipt as a duplicate-prevention
                    # tombstone. Never assign one delivery to two local IDs.
                    original = conn.execute("SELECT external_id FROM SyncOrders WHERE scope=? AND internal_id=?",
                        (manifest["old_scope"], retained)).fetchone()
                    receipt_outcomes = [item for item in manifest["deliveries"] if item["internal_id"] == internal]
                    if not original or not original[0] or len(receipt_outcomes) != 1 or receipt_outcomes[0]["state"] != "TERMINAL" or receipt_outcomes[0].get("native_delivery_id") or receipt_outcomes[0].get("native_job_id"):
                        raise ValueError("A linked original CDS ID must retain its mapping and a separate receipt tombstone")
                    conn.execute("UPDATE SyncOrders SET submission='TERMINAL' WHERE scope=? AND internal_id=?", (manifest["old_scope"], internal))
                conn.execute("UPDATE SyncOrders SET source_review=NULL,last_error=NULL WHERE scope=? AND internal_id=?", (manifest["old_scope"], internal))
        # Unknown receipts must survive cutover as held work, never as terminal/queued.
        for (internal,) in conn.execute("SELECT internal_id FROM UpgradeCdsFiles WHERE scope=? AND resolution IS NULL AND internal_id IS NOT NULL", (manifest["old_scope"],)):
            matches = [item for item in manifest["deliveries"] if item["internal_id"] == internal]
            if len(matches) != 1 or matches[0]["state"] != "NEEDS_REVIEW" or matches[0].get("native_delivery_id") or matches[0].get("native_job_id"):
                raise ValueError("Unresolved CDS receipts must remain held during cutover")
        conn.commit()
    finally:
        conn.close()
    report = migrate(str(staged), manifest, apply=True)
    result.backups.append(report["backup"])
    conn = _connect(staged)
    try:
        conn.execute("UPDATE UpgradeCdsFiles SET scope=? WHERE scope=?", (manifest["new_scope"], manifest["old_scope"]))
        # Preserve source-continuity adoption across a transport-only cutover.
        # The first live Farmax branch observation may happen after installer cutover.
        conn.execute("UPDATE UpgradeInstallation SET scope=? WHERE id=1", (manifest["new_scope"],))
        conn.execute("UPDATE SyncScope SET source_timezone=? WHERE id=1", (configured_source_timezone(new_settings),))
        conn.execute("CREATE TABLE IF NOT EXISTS UpgradePackages(identity TEXT PRIMARY KEY, authority TEXT NOT NULL, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO UpgradePackages(identity,authority) VALUES(?,?)", (identity, json.dumps(package["authority"])))
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Migrated database integrity check failed")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    # All supporting files must exist before publishing the native config.
    _cds_observation_options(UpgradeResult(new_settings, str(staged), result.installation_id))
    # Ensure the complete old configuration remains available, including unknown keys.
    backup = _new_path(config_path.parent, config_path.name + ".pre-native-", ".yml")
    _write_bytes(backup, raw)
    result.backups.append(str(backup))
    candidate = _new_path(config_path.parent, config_path.name + ".publish-", ".tmp")
    try:
        _write_bytes(candidate, yaml.safe_dump(proposed, allow_unicode=True, sort_keys=False).encode("utf-8"))
        if config_path.read_bytes() != raw:
            raise ValueError("Configuration changed while preparing cutover")
        from upgrade_auth import bind_pre_cutover_auth
        bind_pre_cutover_auth(Path(result.token_path), result.settings.auth, result.backups)
        if config_path.read_bytes() != raw:
            raise ValueError("Configuration changed while binding the pre-cutover session")
        os.replace(str(candidate), str(config_path))  # Sole native activation commit point.
    finally:
        if candidate.exists():
            candidate.unlink()
    result.settings = new_settings
    result.database_path = str(staged)
    result.native_applied = True


def _cds_observation_options(result):
    """Use the journal inventory instead of a second, later exclusion snapshot.

    Pass result.cds_baseline_path to CdsStrategy(baseline_path=...). Its empty
    exclusion list lets every file reach durable acceptance/review, including
    files arriving between installer adoption and application startup. Journal
    source_review holds, not watcher exclusion, prevent historical duplicates.
    """
    if result.settings.target_system.value != "CDS":
        return
    path = Path(result.database_path + ".upgrade-cds-observation.json")
    folder = os.path.normcase(os.path.abspath(result.settings.folder_to_watch))
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("folder") != folder or data.get("unobserved_files") != {}:
            raise ValueError("Upgrade CDS observation boundary changed")
    else:
        candidate = _new_path(path.parent, path.name + ".", ".tmp")
        try:
            _write_bytes(candidate, json.dumps({"version": 1, "folder": folder,
                "observed_at_ns": time.time_ns(), "unobserved_files": {}}).encode("utf-8"))
            os.replace(str(candidate), str(path))
        finally:
            if candidate.exists():
                candidate.unlink()
    result.cds_baseline_path = str(path)


def upgrade_installation(config_path, bundle_dir=None, package_path=None):
    """Return effective Settings + diagnostics. Call before build_services.

    Adoption errors raise (no ingestion should start with an ambiguous binding).
    Optional package errors are returned in package_error and leave the adopted
    legacy settings/database usable. Display/log instructions; never hide holds.
    Caller MUST hold the application instance lock until shutdown.
    """
    from config import Settings
    config_path = Path(config_path).resolve()
    bundle_dir = Path(bundle_dir).resolve() if bundle_dir else config_path.parent.parent
    with FileLock(str(config_path) + ".upgrade.lock", timeout=0):
        raw = config_path.read_bytes()
        document = yaml.safe_load(raw.decode("utf-8-sig"))
        settings = Settings.model_validate(document)
        database = Path(settings.sqlite_path)
        if not database.is_absolute():
            database = bundle_dir / database
        database = database.resolve()
        result = UpgradeResult(settings, str(database), "", token_path=str(bundle_dir / "resources" / "token.txt"))
        result.installation_id = _adopt(database, settings, result)
        package_path = Path(package_path) if package_path else config_path.with_name("native-upgrade.json")
        if package_path.exists():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8-sig"))
                _native_cutover(config_path, raw, document, database, package, result)
            except Exception as exc:
                # Keep the technical cause for CLI/support diagnostics; main
                # logs instructions at INFO and QLogHandler shows them verbatim.
                result.package_error = str(exc)
                result.instructions.append(
                    "Não foi possível concluir a migração para a nova conexão do Velide. "
                    "A configuração atual continua ativa e os dados foram preservados. "
                    "Solicite ao suporte a revisão do pacote de atualização.")
        conn = _connect(result.database_path, readonly=True)
        try:
            result.unresolved_cds = conn.execute("SELECT count(*) FROM UpgradeCdsFiles WHERE resolution IS NULL").fetchone()[0]
        finally:
            conn.close()
        if result.unresolved_cds:
            pending = ("1 arquivo antigo ainda precisa" if result.unresolved_cds == 1 else
                       "{} arquivos antigos ainda precisam".format(result.unresolved_cds))
            result.instructions.append(
                "Histórico do CDS: {} de confirmação de envio. "
                "Esse histórico foi preservado e não será reenviado automaticamente, para evitar duplicidade. "
                "Confira os pedidos no Velide e solicite ao suporte a confirmação desse histórico.".format(pending))
        _cds_observation_options(result)
        return result
