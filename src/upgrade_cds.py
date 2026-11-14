"""Conservative, durable inventory of CDS files predating deterministic IDs.

The original client persisted random IDs, not receipt filenames. Unknown files
are unresolved work, NOT a processed baseline. Only authoritative evidence can
release them. No source file is changed or deleted.
"""
import hashlib
import json
from pathlib import Path

from utils.source_identity import cds_file_identity


def create_inventory_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS UpgradeCdsFiles (
        scope TEXT NOT NULL, filename TEXT NOT NULL, digest TEXT NOT NULL,
        internal_id TEXT, original BLOB NOT NULL, error TEXT,
        resolution TEXT, evidence TEXT,
        PRIMARY KEY(scope,filename))""")


def inventory_cds(conn, scope, folder):
    """Run once, before ingestion. Incomplete/unreadable inventories abort adoption."""
    from models.cds_order_model import CdsOrder

    create_inventory_table(conn)
    for path in sorted(Path(folder).iterdir()):
        name = path.name.lower()
        if not (name.startswith("ent") and name.endswith(".json") and path.is_file()):
            continue
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("CDS file changed during upgrade inventory: " + name)
        internal, error = None, None
        try:
            order = CdsOrder.model_validate(json.loads(raw.decode("windows-1252")))
            internal = cds_file_identity(name, order.horario_pedido)
        except (ValueError, TypeError) as exc:
            error = type(exc).__name__ + ": original receipt not yet readable"
        known = internal and conn.execute(
            """SELECT 1 FROM SyncOrders WHERE scope=? AND internal_id=?
            UNION ALL SELECT 1 FROM DeliveryMapping WHERE internal_delivery_id=? LIMIT 1""",
            (scope, internal, internal)
        ).fetchone()
        conn.execute("""INSERT INTO UpgradeCdsFiles
            (scope,filename,digest,internal_id,original,error,resolution)
            VALUES(?,?,?,?,?,?,?)""", (scope, name, hashlib.sha256(raw).hexdigest(),
                                       internal, raw, error, "JOURNALED" if known else None))
        if internal and not known:
            hold = json.dumps({"kind": "CDS_UPGRADE_UNRESOLVED", "filename": name})
            conn.execute("""INSERT INTO SyncOrders
                (scope,internal_id,submission,source_review,last_error)
                VALUES(?,?,'NEEDS_REVIEW',?,?)""", (scope, internal, hold,
                "Pre-upgrade CDS receipt: server submission outcome is unknown"))


def cds_upgrade_hold(conn, scope, internal_id, source_file=None):
    """Journal.accept hook, inside its transaction, before making work dispatchable.

    Return a review object (or None). Persist it as source_review/NEEDS_REVIEW;
    retain/accept the payload so the work is visible. Include source_file in the
    source payload to scope holds for formerly incomplete files precisely. Until
    then an unreadable original receipt conservatively holds unknown CDS IDs.
    """
    if not scope.split("|")[1] == "CDS" or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='UpgradeCdsFiles'"
    ).fetchone():
        return None
    row = conn.execute("""SELECT filename,error FROM UpgradeCdsFiles
        WHERE scope=? AND resolution IS NULL AND
        (internal_id=? OR (internal_id IS NULL AND (? IS NULL OR filename=?))) LIMIT 1""",
        (scope, internal_id, source_file, source_file.lower() if source_file else None)).fetchone()
    if row:
        return {"kind": "CDS_UPGRADE_UNRESOLVED", "filename": row[0],
                "message": "Original CDS submission needs authoritative resolution"}
    return None


def resolve_cds_inventory(conn, scope, entries):
    """Apply package outcomes tied to exact inventoried bytes, in caller's transaction.

    Outcomes: PROCESSED (authoritative terminal receipt), NOT_SUBMITTED (safe
    queue), LINKED (manifest maps this ID, or explicitly named retained_internal_id
    from the original random-ID client). Unknown files
    omitted from the package remain held. No customer/address matching.
    """
    seen = set()
    for entry in entries:
        name = entry["filename"].lower()
        if name in seen:
            raise ValueError("Repeated CDS resolution")
        seen.add(name)
        row = conn.execute("""SELECT digest,internal_id,resolution FROM UpgradeCdsFiles
            WHERE scope=? AND filename=?""", (scope, name)).fetchone()
        if not row or row[0] != entry["sha256"] or not row[1]:
            raise ValueError("CDS resolution must match an inventoried readable receipt")
        outcome = entry["outcome"]
        if outcome not in ("PROCESSED", "NOT_SUBMITTED", "LINKED") or not entry.get("evidence"):
            raise ValueError("CDS resolution requires an authoritative outcome and evidence")
        if row[2] and row[2] != outcome:
            raise ValueError("CDS receipt already has a different outcome")
        order = conn.execute("""SELECT submission,attempts,request,external_id,job_id,cancellation,source_review
            FROM SyncOrders WHERE scope=? AND internal_id=?""", (scope, row[1])).fetchone()
        review = json.loads(order[6] or "{}") if order else {}
        if not row[2] and (not order or review.get("kind") != "CDS_UPGRADE_UNRESOLVED"
                          or order[1:6] != (0, None, None, None, "NONE")):
            raise ValueError("CDS resolution cannot replace existing submitted work")
        if outcome != "LINKED":
            conn.execute("""UPDATE SyncOrders SET submission=?,source_review=NULL,last_error=NULL
                WHERE scope=? AND internal_id=?""",
                ("TERMINAL" if outcome == "PROCESSED" else "QUEUED", scope, row[1]))
        conn.execute("""UPDATE UpgradeCdsFiles SET resolution=?,evidence=?
            WHERE scope=? AND filename=?""", (outcome, json.dumps(entry["evidence"]), scope, name))
