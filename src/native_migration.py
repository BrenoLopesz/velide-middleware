"""Offline, manifest-driven local cutover. Run with Middleware stopped."""
import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime
from api.sync_journal import SyncJournal
from utils.source_identity import cds_file_identity


def processed_cds_ids(manifest):
    result = []
    for entry in manifest.get("processed_cds_files", []):
        filename = entry["filename"]
        if os.path.basename(filename) != filename or not filename.lower().startswith("ent") or not filename.lower().endswith(".json"):
            raise ValueError("Processed CDS entries must identify an ent*.json basename")
        result.append(cds_file_identity(filename, datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))))
    if len(result) != len(set(result)):
        raise ValueError("Repeated CDS baseline identity")
    return result


def prepare_legacy(path, manifest):
    """Upgrade local storage and record reviewed CDS history without starting I/O."""
    if not os.path.isfile(path) or manifest.get("version") != 1 or len(manifest["old_scope"].split("|")) != 2:
        raise ValueError("An existing legacy database and version-1 manifest are required")
    validate_source_fingerprint(manifest)
    ids = processed_cds_ids(manifest)
    if ids and not manifest["old_scope"].endswith("|CDS"):
        raise ValueError("CDS file baseline requires a CDS source")
    journal = SyncJournal(path, manifest["old_scope"])
    journal.restore()
    with journal._db() as db:
        if manifest.get("source_identity"):
            previous = db._get_conn().execute("SELECT source_identity FROM SyncScope WHERE id=1").fetchone()[0]
            if previous is not None and previous != manifest["source_identity"]:
                raise ValueError("Source identity changed; restore/replace qualification is required")
            db._get_conn().execute("UPDATE SyncScope SET source_identity=? WHERE id=1", (manifest["source_identity"],))
        if manifest.get("farmax_branch_id") is not None:
            from connectors.farmax.identity import normalize_farmax_id
            branch = normalize_farmax_id(manifest["farmax_branch_id"])
            previous = db._get_conn().execute("SELECT farmax_branch FROM SyncScope WHERE id=1").fetchone()[0]
            if previous is not None and previous != branch:
                raise ValueError("Qualified Farmax branch cannot be silently replaced")
            db._get_conn().execute("UPDATE SyncScope SET farmax_branch=? WHERE id=1", (branch,))
        for internal in ids:
            db._get_conn().execute("INSERT OR IGNORE INTO SyncOrders(scope,internal_id,submission) VALUES(?,?,'TERMINAL')",
                                  (manifest["old_scope"], internal))
    return {"prepared": True, "processed_cds_files": len(ids), "scope": manifest["old_scope"]}


def validate_source_fingerprint(manifest):
    if manifest.get("source_identity") is not None and (not isinstance(manifest["source_identity"], str) or len(manifest["source_identity"]) != 64
                                                       or any(c not in "0123456789abcdef" for c in manifest["source_identity"])):
        raise ValueError("Invalid qualified source configuration fingerprint")


def validate_manifest(conn, manifest):
    if manifest.get("version") != 1:
        raise ValueError("Unsupported migration manifest version")
    validate_source_fingerprint(manifest)
    scope = manifest["new_scope"].split("|")
    if len(scope) != 4 or not scope[0].rstrip("/").endswith("/graphql") or scope[1] not in ("Farmax", "CDS"):
        raise ValueError("Invalid native scope")
    uuid.UUID(scope[2])
    uuid.UUID(scope[3])
    actual_scope = conn.execute("SELECT scope FROM SyncScope WHERE id=1").fetchone()
    if not actual_scope or actual_scope[0] != manifest["old_scope"]:
        raise ValueError("Manifest does not match the installed source scope")
    if conn.execute("SELECT 1 FROM SyncEffects WHERE state='PENDING' LIMIT 1").fetchone():
        raise ValueError("Resolve all outstanding legacy ERP effects before cutover")
    if conn.execute("SELECT 1 FROM DeliveryMapping WHERE status='EM_ANDAMENTO' LIMIT 1").fetchone():
        raise ValueError("Finish active legacy routes before cutover")
    actual = {row[0] for row in conn.execute("SELECT internal_id FROM SyncOrders")}
    actual.update(row[0] for row in conn.execute("SELECT internal_delivery_id FROM DeliveryMapping"))
    entries = manifest["deliveries"]
    baseline = set(processed_cds_ids(manifest))
    if baseline and scope[1] != "CDS":
        raise ValueError("CDS file baseline requires a CDS target")
    for internal in baseline:
        if conn.execute("SELECT 1 FROM DeliveryMapping WHERE internal_delivery_id=?", (internal,)).fetchone():
            raise ValueError("Mapped CDS orders require explicit delivery manifest entries")
        row = conn.execute("SELECT submission FROM SyncOrders WHERE internal_id=?", (internal,)).fetchone()
        if row and row[0] != "TERMINAL":
            raise ValueError("An active CDS order cannot be declared already processed")
    actual -= baseline
    if len({entry["internal_id"] for entry in entries}) != len(entries) or {entry["internal_id"] for entry in entries} != actual:
        raise ValueError("Every retained order must have exactly one manifest outcome")
    for entry in entries:
        if entry["state"] not in ("QUEUED", "ACCEPTED", "IMPORTED", "TERMINAL", "NEEDS_REVIEW"):
            raise ValueError("Invalid retained order outcome")
        for field in ("native_delivery_id", "native_job_id"):
            if entry.get(field):
                uuid.UUID(entry[field])
        old = conn.execute("SELECT external_delivery_id FROM DeliveryMapping WHERE internal_delivery_id=?", (entry["internal_id"],)).fetchone()
        work = conn.execute("SELECT submission,attempts,request,job_id,cancellation FROM SyncOrders WHERE internal_id=?", (entry["internal_id"],)).fetchone()
        if (old[0] if old else None) != entry.get("legacy_delivery_id"):
            raise ValueError("Legacy mapping differs from manifest")
        if entry["state"] == "QUEUED" and (old or not work or work != ("QUEUED", 0, None, None, "NONE")):
            raise ValueError("Only proven never-submitted work can be requeued; retain or resolve uncertain legacy submissions")
        if entry["state"] == "ACCEPTED" and work and work[4] not in ("NONE", "ACKNOWLEDGED"):
            raise ValueError("Resolve cancellation intent before migrating an accepted import")
        if entry["state"] == "IMPORTED" and not entry.get("native_delivery_id"):
            raise ValueError("Imported outcome requires a native delivery ID")
        if entry["state"] == "ACCEPTED" and not entry.get("native_job_id"):
            raise ValueError("Accepted outcome requires a native job ID")
    actual_couriers = dict(conn.execute("SELECT local_id,velide_id FROM DeliverymenMapping"))
    couriers = manifest["couriers"]
    if len({entry["local_id"] for entry in couriers}) != len(couriers) or {entry["local_id"] for entry in couriers} != set(actual_couriers):
        raise ValueError("Every courier mapping must have exactly one manifest outcome")
    for entry in couriers:
        if actual_couriers[entry["local_id"]] != entry["legacy_id"]:
            raise ValueError("Legacy courier mapping differs from manifest")
        uuid.UUID(entry["native_id"])


def migrate(path, manifest, apply=False):
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()
    if not os.path.isfile(path):
        raise ValueError("Database does not exist")
    conn = sqlite3.connect(Path(path).absolute().as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='SyncMigrations'").fetchone():
            if conn.execute("SELECT 1 FROM SyncMigrations WHERE identity=?", (identity,)).fetchone():
                return {"already_applied": True, "identity": identity}
        validate_manifest(conn, manifest)
        report = {"identity": identity, "orders": len(manifest["deliveries"]), "couriers": len(manifest["couriers"]), "applied": False}
        if not apply:
            return report
        backup_path = path + ".native-" + identity[:12] + ".db"
        if os.path.exists(backup_path):
            raise ValueError("Backup already exists; inspect a prior interrupted migration")
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        conn.execute("BEGIN IMMEDIATE")
        validate_manifest(conn, manifest)
        for table in ("SyncOrders", "SyncEffects", "SyncSource", "SyncCheckpoints"):
            conn.execute("UPDATE " + table + " SET scope=? WHERE scope=?", (manifest["new_scope"], manifest["old_scope"]))
        for entry in manifest["deliveries"]:
            internal = entry["internal_id"]
            conn.execute("INSERT OR IGNORE INTO SyncOrders(scope,internal_id,submission) VALUES(?,?,?)", (manifest["new_scope"], internal, entry["state"]))
            conn.execute("""UPDATE SyncOrders SET external_id=?,job_id=?,submission=?,request=NULL,retry_at=0
                WHERE scope=? AND internal_id=?""", (entry.get("native_delivery_id"), entry.get("native_job_id"), entry["state"], manifest["new_scope"], internal))
            if entry.get("native_delivery_id"):
                conn.execute("UPDATE DeliveryMapping SET external_delivery_id=? WHERE internal_delivery_id=?", (entry["native_delivery_id"], internal))
            else:
                # Full old mapping remains in the backup and manifest audit.
                # SyncOrders retains the terminal/unresolved identity.
                conn.execute("DELETE FROM DeliveryMapping WHERE internal_delivery_id=?", (internal,))
        for entry in manifest["couriers"]:
            conn.execute("UPDATE DeliverymenMapping SET velide_id=? WHERE local_id=?", (entry["native_id"], entry["local_id"]))
        for internal in processed_cds_ids(manifest):
            conn.execute("INSERT OR IGNORE INTO SyncOrders(scope,internal_id,submission) VALUES(?,?,'TERMINAL')", (manifest["new_scope"], internal))
        conn.execute("UPDATE SyncScope SET scope=? WHERE id=1", (manifest["new_scope"],))
        conn.execute("CREATE TABLE IF NOT EXISTS SyncMigrations(identity TEXT PRIMARY KEY,manifest TEXT NOT NULL,applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO SyncMigrations(identity,manifest) VALUES(?,?)", (identity, json.dumps(manifest, sort_keys=True)))
        conn.commit()
        return dict(report, applied=True, backup=backup_path)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("manifest")
    parser.add_argument("--apply", action="store_true", help="Commit the validated migration; default is read-only validation")
    parser.add_argument("--prepare", action="store_true", help="Prepare the legacy schema/baseline offline, without starting integration")
    args = parser.parse_args()
    with open(args.manifest, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if args.prepare and args.apply:
        parser.error("Use --prepare and --apply as separate steps")
    print(json.dumps(prepare_legacy(args.database, manifest) if args.prepare else migrate(args.database, manifest, args.apply), indent=2))
