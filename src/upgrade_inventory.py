"""Credential-free inventory for centralized, planned native server migration.

Exports retained business/source evidence, never configuration credentials, token
files, raw GraphQL documents, or arbitrary request headers. Caller holds the app
instance lock. Export may adopt an old SQLite schema but never consumes a native
package or changes the installed configuration.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from filelock import FileLock
import yaml

from local_upgrade import UpgradeResult, _adopt, _connect, installation_binding
from utils.source_identity import configured_scope, configured_source_identity, configured_source_timezone
from upgrade_state import scoped_upgrade_tables


_SECRET_KEY = re.compile(r"password|passwd|pwd|token|secret|credential|authorization|cookie|api.?key|^auth$|^headers$|connection.?string|^dsn$", re.I)
_BUSINESS_FIELDS = {
    "internal_id", "customerName", "customerContact", "address", "address2", "reference",
    "neighbourhood", "createdAt", "source_context", "sourceContext", "idempotencyKey",
    "rawAddress", "rawComplemento", "rawReference", "rawNeighbourhood", "createdAtIntent",
    "timestamp", "offset", "metadata",
    "nome_cliente", "endereco", "horario_pedido", "contato_cliente", "complemento",
    "referencia", "valor_pedido", "integration", "externalOrderId", "connectionId",
}


def _scrub(value, password=None):
    if isinstance(value, dict):
        return {key: _scrub(item, password) for key, item in value.items() if not _SECRET_KEY.search(key)}
    if isinstance(value, list):
        return [_scrub(item, password) for item in value]
    if isinstance(value, str):
        if value.lower().startswith(("bearer ", "basic ")) or re.search(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", value):
            return "[REDACTED]"
        if password and password in value:
            return value.replace(password, "[REDACTED]")
    return value


def _json(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None  # Opaque/malformed bodies are not exported as arbitrary text.


def _business(value, password):
    if not isinstance(value, dict):
        return None
    # Legacy requests can wrap an input in variables; exclude query/header text.
    if isinstance(value.get("variables"), dict):
        value = value["variables"]
    if isinstance(value.get("input"), dict):
        value = value["input"]
    return _scrub({key: item for key, item in value.items() if key in _BUSINESS_FIELDS}, password)


def _rows(conn, sql):
    cursor = conn.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def inventory_state_fingerprint(conn):
    """Digest the actual retained state, not its redacted exported representation.

    Excludes export timestamps/audit tables. Includes attempted work, cancellation,
    effects, source observations, original receipt bytes and exact legacy mappings.
    This can be checked again by the consumer without exposing any secret values.
    """
    state = {}
    for table in ("SyncScope", "DeliveryMapping", "DeliverymenMapping", "UpgradeCdsFiles") + scoped_upgrade_tables(conn):
        encoded = []
        for row in _rows(conn, "SELECT * FROM " + table):
            row = {key: {"bytes_sha256": hashlib.sha256(value).hexdigest()} if isinstance(value, bytes) else value
                   for key, value in row.items()}
            encoded.append(json.dumps(row, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
        state[table] = sorted(encoded)
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def export_upgrade_inventory(config_path, bundle_dir=None):
    from config import Settings

    config_path = Path(config_path).resolve()
    root = Path(bundle_dir).resolve() if bundle_dir else config_path.parent.parent
    with FileLock(str(config_path) + ".upgrade.lock", timeout=0):
        raw = config_path.read_bytes()
        settings = Settings.model_validate(yaml.safe_load(raw.decode("utf-8-sig")))
        endpoint = urlsplit(settings.api.velide_server)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("Inventory cannot export an endpoint containing credentials/query parameters")
        if settings.api.native:
            raise ValueError("Upgrade inventory is for an installed legacy source, before native cutover")
        database = (root / settings.sqlite_path).resolve()
        result = UpgradeResult(settings, str(database), "")
        _adopt(database, settings, result)
        # A pre-cutover rollout can export, resume the original legacy executable,
        # then export again. That executable only writes DeliveryMapping. Restore
        # any newly retained mappings before taking the final inventory snapshot.
        from api.sync_journal import SyncJournal
        SyncJournal(str(database), configured_scope(settings), configured_source_identity(settings),
                    configured_source_timezone(settings)).restore()
        conn = _connect(database, readonly=True)
        try:
            conn.execute("BEGIN")
            bound = conn.execute("SELECT source_timezone,farmax_branch FROM SyncScope WHERE id=1").fetchone()
            password = settings.farmax.password if settings.farmax else None
            mappings = {row["internal_delivery_id"]: row for row in _rows(conn, "SELECT * FROM DeliveryMapping")}
            orders = []
            for row in _rows(conn, "SELECT * FROM SyncOrders ORDER BY internal_id"):
                mapping = mappings.get(row["internal_id"], {})
                review = _json(row["source_review"])
                orders.append({
                    "internal_id": row["internal_id"], "legacy_delivery_id": mapping.get("external_delivery_id"),
                    "legacy_status": mapping.get("status"), "journal_external_id": row["external_id"],
                    "submission": row["submission"], "cancellation": row["cancellation"],
                    "attempts": row["attempts"], "has_request": row["request"] is not None, "job_id": row["job_id"],
                    "source_receipt_raw": row["source_receipt_raw"],
                    "source_review_kind": review.get("kind") if isinstance(review, dict) else None,
                    "add_attempts": row.get("add_attempts", 0), "delete_attempts": row.get("delete_attempts", 0),
                    "farmax_progress": _scrub(_json(row.get("farmax_progress")), password),
                    "payload": _business(_json(row["payload"]), password),
                    "request": _business(_json(row["request"]), password),
                })
            files = []
            for row in _rows(conn, "SELECT * FROM UpgradeCdsFiles ORDER BY filename"):
                original = row["original"]
                files.append({"filename": row["filename"], "sha256": row["digest"],
                    "internal_id": row["internal_id"], "resolution": row["resolution"],
                    "parse_error": row["error"] is not None, "byte_length": len(original),
                    "receipt": _business(_json(original.decode("windows-1252", errors="replace")), password)})
            observations = []
            for row in _rows(conn, "SELECT source_id,payload,resolved FROM SyncSource ORDER BY source_id"):
                observations.append({"source_id": row["source_id"], "resolved": bool(row["resolved"]),
                                     "source_context": _scrub(_json(row["payload"]), password)})
            inventory = {
                "version": 1, "kind": "velide-upgrade-inventory",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "binding": installation_binding(config_path, database),
                "state_sha256": inventory_state_fingerprint(conn),
                "source": {"target_system": settings.target_system.value, "timezone": bound[0], "farmax_branch": bound[1]},
                "orders": orders,
                "couriers": _rows(conn, "SELECT local_id,velide_id AS legacy_id FROM DeliverymenMapping ORDER BY local_id"),
                "cds_files": files, "source_observations": observations,
                "pending_effects": _rows(conn, "SELECT sequence,internal_id,operation,attempts FROM SyncEffects WHERE state='PENDING' ORDER BY sequence"),
            }
            if "SyncFarmaxEvents" in scoped_upgrade_tables(conn):
                inventory["farmax_events"] = [dict(row, evidence=_scrub(_json(row["evidence"]), password))
                    for row in _rows(conn, "SELECT sequence,internal_id,evidence,observed_at FROM SyncFarmaxEvents ORDER BY sequence")]
            if config_path.read_bytes() != raw:
                raise ValueError("Configuration changed during inventory export")
            return inventory
        finally:
            conn.close()
