"""Transactional obligations shared by source ingestion, dispatch and ERP effects."""
import json
import time
import logging
import os
from threading import RLock, Lock
from contextlib import contextmanager

from api.sqlite_manager import SQLiteManager, DeliveryStatus
from connectors.farmax.identity import normalize_farmax_id
from api.source_payload import canonical_payload, comparable_payload
from connectors.farmax.errors import FarmaxEvidenceError, FarmaxMappingMissing


_journal_locks = {}
_journal_locks_guard = Lock()


def _source_lock(path):
    with _journal_locks_guard:
        return _journal_locks.setdefault(os.path.normcase(os.path.abspath(path)), RLock())


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class SyncJournal:
    def __init__(self, path, scope, source_identity=None, source_timezone=None):
        self.path, self.scope = path, scope
        self.source_identity = source_identity
        self.source_timezone = source_timezone
        self._effect_lock = _source_lock(path)

    @contextmanager
    def _db(self):
        # Serialize local invalidation with the final ERP check/write/commit.
        # Reentrant for repository branch checks on the same worker thread.
        with self._effect_lock:
            with self._transaction_db() as db:
                yield db

    @contextmanager
    def _transaction_db(self):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            existing_scope = conn.execute("SELECT scope FROM SyncScope WHERE id=1").fetchone()
            if not existing_scope and len(self.scope.split("|")) == 4:
                import uuid
                for row in conn.execute("SELECT external_delivery_id FROM DeliveryMapping UNION ALL SELECT velide_id FROM DeliverymenMapping"):
                    try:
                        uuid.UUID(row[0])
                    except ValueError:
                        raise ValueError("Legacy mappings require a native migration manifest")
            conn.execute("INSERT OR IGNORE INTO SyncScope(id,scope) VALUES(1,?)", (self.scope,))
            bound = conn.execute("SELECT scope FROM SyncScope WHERE id=1").fetchone()[0]
            if bound != self.scope:
                raise ValueError("Database belongs to another endpoint/source; apply a migration manifest first")
            if self.source_timezone:
                import pytz
                pytz.timezone(self.source_timezone)
                previous_zone = conn.execute("SELECT source_timezone FROM SyncScope WHERE id=1").fetchone()[0]
                if previous_zone is not None and previous_zone != self.source_timezone:
                    raise ValueError("Bound source timezone changed; qualify original receipts offline")
                conn.execute("UPDATE SyncScope SET source_timezone=? WHERE id=1", (self.source_timezone,))
            if self.source_identity:
                previous = conn.execute("SELECT source_identity FROM SyncScope WHERE id=1").fetchone()[0]
                if previous != self.source_identity:
                    has_work = conn.execute("SELECT 1 FROM SyncOrders UNION ALL SELECT 1 FROM SyncSource UNION ALL SELECT 1 FROM DeliveryMapping LIMIT 1").fetchone()
                    if previous is not None or has_work:
                        raise ValueError("Physical source configuration is unverified or changed; qualify it in the offline migration manifest")
                    conn.execute("UPDATE SyncScope SET source_identity=? WHERE id=1", (self.source_identity,))
            yield db

    def _rows(self, conn, sql, args=()):
        cursor = conn.execute(sql, (self.scope,) + tuple(args))
        names = [field[0] for field in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def bind_farmax_branch(self, branch):
        with self._db() as db:
            conn = db._get_conn()
            bound = conn.execute("SELECT farmax_branch FROM SyncScope WHERE id=1").fetchone()[0]
            if bound == branch:
                return
            if bound is not None:
                raise ValueError("Farmax local branch changed; use a separately qualified source binding")
            from local_upgrade import adopt_observed_farmax_branch
            if adopt_observed_farmax_branch(conn, self.scope, branch):
                return
            rows = self._rows(conn,"SELECT payload FROM SyncOrders WHERE scope=?")
            contexts = [(json.loads(row["payload"] or "{}").get("source_context") or {}).get("branch_id") for row in rows]
            if contexts and any(value != branch for value in contexts):
                raise ValueError("Existing work has no verified branch provenance; qualify its branch offline")
            conn.execute("UPDATE SyncScope SET farmax_branch=? WHERE id=1",(branch,))

    def restore(self):
        with self._db() as db:
            conn = db._get_conn()
            # Preserve pre-upgrade identities, including unresolved MISSING/FAILED.
            for external, internal, status in db.get_all_deliveries():
                if len(self.scope.split("|")) == 4:
                    import uuid
                    try:
                        uuid.UUID(external)
                    except ValueError:
                        raise ValueError("Legacy delivery mapping requires a native migration manifest")
                state = "TERMINAL" if status in (DeliveryStatus.CANCELLED, DeliveryStatus.DELIVERED) else "IMPORTED"
                conn.execute("""INSERT OR IGNORE INTO SyncOrders
                    (scope, internal_id, external_id, submission) VALUES (?, ?, ?, ?)""",
                             (self.scope, internal, external, state))
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=?")

    def accept(self, internal_id, payload, source_file=None):
        conflict = False
        with self._db() as db:
            conn = db._get_conn()
            zone = conn.execute("SELECT source_timezone FROM SyncScope WHERE id=1").fetchone()[0]
            raw_receipt = payload.get("createdAt")
            allow_naive = len(self.scope.split("|")) == 2
            normalized = canonical_payload(payload, zone, allow_naive)
            conn.execute("""INSERT OR IGNORE INTO SyncOrders(scope,internal_id,payload,source_receipt_raw)
                VALUES(?,?,?,?)""", (self.scope, internal_id, encode(normalized), raw_receipt))
            # Restoration may supply the missing source snapshot, but never change
            # an already accepted payload or reset a tombstone.
            conn.execute("""UPDATE SyncOrders SET payload=? WHERE scope=? AND internal_id=?
                AND payload IS NULL""", (encode(normalized), self.scope, internal_id))
            row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            original = json.loads(row["payload"])
            before = comparable_payload(original, zone, allow_naive)
            after = comparable_payload(normalized, zone, allow_naive)
            if before != after:
                conflict = True
                self._latch_source_review(conn, internal_id, {"kind": "SOURCE_CONFLICT",
                    "message": "Source payload conflicts with an already accepted order", "observed_payload": normalized})
            else:
                conn.execute("UPDATE SyncOrders SET payload=?,source_receipt_raw=COALESCE(source_receipt_raw,?) WHERE scope=? AND internal_id=?",
                    (encode(canonical_payload(original, zone, allow_naive)), raw_receipt, self.scope, internal_id))
                row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            if self.scope.split("|")[-1] == "CDS" or "|CDS|" in self.scope:
                from upgrade_cds import cds_upgrade_hold
                hold = cds_upgrade_hold(conn, self.scope, internal_id, source_file)
                if hold and not row["source_review"]:
                    self._latch_source_review(conn, internal_id, hold)
                    conn.execute("UPDATE SyncOrders SET submission='NEEDS_REVIEW' WHERE scope=? AND internal_id=?", (self.scope, internal_id))
                    row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
        # The hold must commit before the caller receives the conflict. Raising
        # inside the transaction would roll back the very protection required.
        if conflict:
            raise ValueError("Source payload conflicts with an already accepted order; durable review hold recorded")
        return row

    def active_orders(self):
        with self._db() as db:
            # Linked terminal deliveries remain observable: native undo/redo can
            # change them after an ERP match was acknowledged.
            return self._rows(db._get_conn(), """SELECT internal_id FROM SyncOrders WHERE scope=?
                AND (external_id IS NOT NULL OR (submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED')) ORDER BY internal_id""")

    def get_order(self, internal):
        with self._db() as db:
            rows = self._rows(db._get_conn(),"SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?",(internal,))
            return rows[0] if rows else None

    def bootstrap_status(self):
        with self._db() as db:
            row = db._get_conn().execute("SELECT payload FROM SyncSource WHERE scope=? AND source_id='native-bootstrap'", (self.scope,)).fetchone()
            return json.loads(row[0]) if row else {"after": "", "complete": False}

    def bootstrap_progress(self, after, complete):
        with self._db() as db:
            db._get_conn().execute("""INSERT INTO SyncSource(scope,source_id,payload,resolved) VALUES(?,'native-bootstrap',?,1)
                ON CONFLICT(scope,source_id) DO UPDATE SET payload=excluded.payload""",
                (self.scope, encode({"after": after, "complete": complete})))

    def freeze_request(self, internal_id, request):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("UPDATE SyncOrders SET request=? WHERE scope=? AND internal_id=? AND request IS NULL",
                         (encode(request), self.scope, internal_id))
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]

    def claim(self, native=False):
        with self._db() as db:
            conn = db._get_conn()
            rows = self._rows(conn, """SELECT * FROM SyncOrders WHERE scope=? AND retry_at<=?
                AND cancellation<>'ACKNOWLEDGED' AND submission<>'TERMINAL'
                AND ((cancellation='REQUESTED' AND (external_id IS NOT NULL OR ? OR
                     (request IS NOT NULL AND payload IS NOT NULL AND submission IN ('SUBMITTING','RETRY','NEEDS_REVIEW'))))
                     OR (submission IN ('QUEUED','SUBMITTING','RETRY') AND payload IS NOT NULL AND source_review IS NULL))
                ORDER BY rowid LIMIT 1""", (time.time(), native))
            if not rows:
                return None
            row = rows[0]
            row["operation"] = "DELETE" if row["cancellation"] == "REQUESTED" and (row["external_id"] or native) else "ADD"
            conn.execute("""UPDATE SyncOrders SET attempts=attempts+1,
                submission=CASE WHEN ?='ADD' THEN 'SUBMITTING' ELSE submission END
                WHERE scope=? AND internal_id=?""", (row["operation"], self.scope, row["internal_id"]))
            row["attempts"] += 1
            counter = "delete_attempts" if row["operation"] == "DELETE" else "add_attempts"
            conn.execute("UPDATE SyncOrders SET " + counter + "=" + counter + "+1 WHERE scope=? AND internal_id=?", (self.scope, row["internal_id"]))
            return row

    def dispatch_is_current(self, internal, attempt, operation):
        with self._db() as db:
            rows = self._rows(db._get_conn(),"SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?",(internal,))
            if not rows or rows[0]["attempts"] != attempt:
                return False
            row = rows[0]
            if operation == "DELETE":
                return row["cancellation"] == "REQUESTED"
            recovery = (len(self.scope.split("|")) == 2 and row["cancellation"] == "REQUESTED"
                        and row["request"] is not None)
            return row["submission"] == "SUBMITTING" and (row["cancellation"] == "NONE" or recovery) and row["source_review"] is None and row["external_id"] is None and row["job_id"] is None

    def imported(self, internal_id, external_id):
        with self._db() as db:
            conn = db._get_conn()
            row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            if row["cancellation"] == "ACKNOWLEDGED" or row["submission"] == "TERMINAL":
                return False
            if row["external_id"] and row["external_id"] != external_id:
                raise ValueError("Conflicting remote delivery mapping")
            conn.execute("""UPDATE SyncOrders SET external_id=?, submission='IMPORTED',
                retry_at=0,last_error=NULL WHERE scope=? AND internal_id=?""",
                         (external_id, self.scope, internal_id))
            existing = db.get_delivery_by_internal_id(internal_id)
            if existing and existing[0] != external_id:
                raise ValueError("Conflicting remote delivery mapping")
            if not existing and not db.add_delivery_mapping(external_id, internal_id, DeliveryStatus.ADDED):
                raise RuntimeError("Could not persist delivery mapping")
        return external_id

    def accepted_job(self, internal_id, job_id, attempt=None):
        with self._db() as db:
            result = db._get_conn().execute("""UPDATE SyncOrders SET job_id=?,submission='ACCEPTED',last_error=NULL,retry_at=0
                WHERE scope=? AND internal_id=? AND external_id IS NULL AND submission<>'TERMINAL'
                AND cancellation<>'ACKNOWLEDGED' AND (? IS NULL OR attempts=?)""", (job_id, self.scope, internal_id, attempt, attempt))
            return result.rowcount == 1

    def needs_review(self, internal_id, operation, error, attempt=None):
        with self._db() as db:
            result = db._get_conn().execute("""UPDATE SyncOrders SET last_error=?,
                submission=CASE WHEN ?='ADD' THEN 'NEEDS_REVIEW' ELSE submission END,
                cancellation=CASE WHEN ?='DELETE' OR (?='ADD' AND cancellation='REQUESTED') THEN 'CONFLICT' ELSE cancellation END
                WHERE scope=? AND internal_id=? AND submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED'
                AND (? IS NULL OR attempts=?) AND (?<>'ADD' OR (external_id IS NULL AND job_id IS NULL))""",
                (error, operation, operation, operation, self.scope, internal_id, attempt, attempt, operation))
            return result.rowcount == 1

    def native_states(self, states, farmax=True):
        """Commit accepted remote facts and their ERP obligations in one transaction."""
        with self._db() as db:
            conn = db._get_conn()
            for state in states:
                internal = state["externalOrderId"]
                known = conn.execute("SELECT remote_revision FROM SyncOrders WHERE scope=? AND internal_id=?", (self.scope, internal)).fetchone()
                revision = state.get("revision")
                if revision is not None and (not isinstance(revision, str) or not revision or any(c not in "0123456789" for c in revision)):
                    raise ValueError("Invalid authoritative revision")
                if known and known[0] is not None and (revision is None or int(revision) < int(known[0])):
                    continue
                if not state["found"]:
                    self._invalidate_effects(conn, internal)
                    conn.execute("UPDATE SyncOrders SET last_error='Remote reference unresolved',remote_state=?,remote_revision=COALESCE(?,remote_revision) WHERE scope=? AND internal_id=?",
                                 (encode(state), revision, self.scope, internal))
                    continue
                source, request = state.get("source"), state.get("request")
                payload = None
                if source and request:
                    payload = encode(canonical_payload({"internal_id": internal, "customerName": source["customerName"],
                        "customerContact": source.get("customerContact"), "address": request["rawAddress"],
                        "address2": request.get("rawComplemento"), "reference": request.get("rawReference"),
                        "neighbourhood": request.get("rawNeighbourhood"), "createdAt": request["createdAtIntent"],
                        "source_context": source.get("sourceContext")}))
                conn.execute("INSERT OR IGNORE INTO SyncOrders(scope,internal_id,payload,submission) VALUES(?,?,?,'ACCEPTED')",
                             (self.scope, internal, payload))
                row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal,))[0]
                external = state.get("deliveryId")
                if row["external_id"] and external and row["external_id"] != external:
                    raise ValueError("Native order mapping conflicts with local identity")
                submission = "IMPORTED" if external else "ACCEPTED"
                error = state.get("error")
                cancellation = row["cancellation"]
                if cancellation == "NONE" and state["cancellation"] in ("REQUESTED", "CONFLICT"):
                    cancellation = state["cancellation"]
                if state.get("importStatus") == "FAILED" and not external:
                    submission = "NEEDS_REVIEW"
                if state.get("retainedOutcome"):
                    submission = "NEEDS_REVIEW" if state["retainedOutcome"] == "UNKNOWN" else "TERMINAL"
                if (state["cancellation"] == "ACKNOWLEDGED" and not external) or state.get("deliveryStatus") == "CANCELLED" or state["deleted"]:
                    submission, cancellation = "TERMINAL", "ACKNOWLEDGED"
                elif cancellation == "ACKNOWLEDGED" and external:
                    error = "Current remote delivery contradicts an acknowledged cancellation; review the subsequent action"
                if external:
                    existing = db.get_delivery_by_internal_id(internal)
                    if existing and existing[0] != external:
                        raise ValueError("Migrate legacy delivery mappings before enabling native synchronization")
                    if not existing and not db.add_delivery_mapping(external, internal, DeliveryStatus.ADDED):
                        raise RuntimeError("Could not save native mapping")
                if farmax:
                    error = self._accept_effect_authority(conn, row, state, cancellation, payload) or error
                elif state.get("deliveryStatus") == "COMPLETED":
                    submission = "TERMINAL"
                if row["source_review"]:
                    error = json.loads(row["source_review"]).get("message") or "Source review is required"
                if farmax and self._completion_observed(conn, internal):
                    submission = "TERMINAL"
                conn.execute("""UPDATE SyncOrders SET submission=?,cancellation=?,external_id=COALESCE(?,external_id),
                    job_id=COALESCE(?,job_id),payload=COALESCE(payload,?),remote_state=?,remote_revision=COALESCE(?,remote_revision),last_error=? WHERE scope=? AND internal_id=?""",
                    (submission, cancellation, external, state.get("jobId"), payload, encode(state), revision, error, self.scope, internal))
                if cancellation == "ACKNOWLEDGED" and submission == "TERMINAL":
                    conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                                 (DeliveryStatus.CANCELLED.value, internal))
            references = [state["externalOrderId"] for state in states]
            if not references:
                return []
            placeholders = ",".join("?" for _ in references)
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id IN (" + placeholders + ")", references)

    def _invalidate_effects(self, conn, internal):
        conn.execute("UPDATE SyncOrders SET effect_generation=effect_generation+1,effect_authority=NULL WHERE scope=? AND internal_id=?", (self.scope, internal))
        conn.execute("UPDATE SyncEffects SET state='SUPERSEDED' WHERE scope=? AND internal_id=? AND state='PENDING'", (self.scope, internal))

    def source_review(self, internal, evidence):
        with self._db() as db:
            conn = db._get_conn()
            self._latch_source_review(conn, internal, evidence)

    def _legacy_adopted(self, conn):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='UpgradeInstallation'").fetchone():
            return False
        return conn.execute("SELECT 1 FROM UpgradeInstallation WHERE id=1 AND scope=? AND legacy_adopted=1", (self.scope,)).fetchone() is not None

    def record_farmax_progress(self, internal, progress):
        from api.farmax_progress import correlate_progress
        internal = normalize_farmax_id(internal)
        with self._db() as db:
            conn = db._get_conn()
            rows = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal,))
            if not rows:
                return {"accepted": False, "request_cancel": False}
            row = rows[0]
            payload = json.loads(row["payload"] or "{}")
            previous = json.loads(row["farmax_progress"] or "{}")
            expected = json.loads(row["source_context"]) if row["source_context"] else payload.get("source_context")
            evidence = correlate_progress(internal, expected, previous, progress, self._legacy_adopted(conn))
            changed = encode(evidence) != encode(previous)
            if changed:
                conn.execute("INSERT INTO SyncFarmaxEvents(scope,internal_id,evidence) VALUES(?,?,?)", (self.scope, internal, encode(evidence)))
                # Failed observations don't erase the last complete item ledger.
                if evidence["accepted"]:
                    conn.execute("UPDATE SyncOrders SET farmax_progress=? WHERE scope=? AND internal_id=?", (encode(evidence), self.scope, internal))
                    if evidence["source_context"].get("delivery_date") is not None:
                        conn.execute("UPDATE SyncOrders SET source_context=? WHERE scope=? AND internal_id=?", (encode(evidence["source_context"]), self.scope, internal))
            if evidence["accepted"] and payload:
                current = evidence["source_context"]
                # Physical removal has no delivery date. Preserve the original
                # receipt identity; no writer will run for cancellation intent.
                if current.get("delivery_date") is not None:
                    payload["source_context"] = current
                    conn.execute("UPDATE SyncOrders SET payload=? WHERE scope=? AND internal_id=?", (encode(payload), self.scope, internal))
            request_cancel = False
            if evidence["accepted"] and evidence["decision"] == "CANCEL":
                if row["cancellation"] == "NONE":
                    unsent = (row["submission"] == "QUEUED" and row["attempts"] == 0 and not row["request"] and not row["job_id"] and not row["external_id"])
                    conn.execute("UPDATE SyncOrders SET cancellation=?,retry_at=0,add_attempts=CASE WHEN external_id IS NULL AND request IS NOT NULL THEN 0 ELSE add_attempts END WHERE scope=? AND internal_id=?",
                                 ("ACKNOWLEDGED" if unsent else "REQUESTED", self.scope, internal))
                    self._invalidate_effects(conn, internal)
                    request_cancel = True
                elif row["cancellation"] == "REQUESTED":
                    request_cancel = True
        if changed:
            logger = logging.getLogger(__name__)
            if evidence["accepted"] and evidence.get("cancelled_items"):
                logger.info("Pedido %s: %s/%s itens cancelados (%.1f%%). %s", internal,
                            evidence["cancelled_items"], evidence["total_items"], evidence["percentage"],
                            "Solicitando cancelamento da entrega." if evidence["decision"] == "CANCEL" else "A entrega permanece ativa.")
            elif evidence.get("decision") in ("UNRESOLVED", "REMOVAL_CANDIDATE") or not evidence["accepted"]:
                logger.warning("Pedido %s: %s", internal, evidence.get("reason"))
        return dict(evidence, changed=changed, request_cancel=request_cancel, external_id=row["external_id"])

    def _latch_source_review(self, conn, internal, evidence):
        row = conn.execute("SELECT source_review FROM SyncOrders WHERE scope=? AND internal_id=?", (self.scope, internal)).fetchone()
        value = encode(evidence) if evidence else None
        if row and row[0] != value:
            self._invalidate_effects(conn, internal)
            conn.execute("UPDATE SyncOrders SET source_review=?,last_error=? WHERE scope=? AND internal_id=?",
                         (value, evidence.get("message") if evidence else None, self.scope, internal))

    def _accept_effect_authority(self, conn, row, state, cancellation, restored_payload):
        internal, route = row["internal_id"], state.get("execution")
        facts = {key: state.get(key) for key in ("deliveryId", "deliveryStatus", "deleted", "execution", "retainedOutcome")}
        old = json.loads(row["effect_authority"]) if row["effect_authority"] else None
        blocked = cancellation != "NONE" or state.get("deleted") or state.get("deliveryStatus") == "CANCELLED" or bool(row["source_review"])
        error = "Source/cancellation conflict requires review" if row["source_review"] or cancellation == "CONFLICT" else None
        if state.get("revision") is None:
            blocked, error = True, "Versioned authoritative state is required"
        elif ((old and old["facts"] != facts and row["remote_revision"] == state["revision"])
              or row["authority_conflict_revision"] == state["revision"]):
            blocked, error = True, "Conflicting lifecycle facts at the same revision"
            conn.execute("UPDATE SyncOrders SET authority_conflict_revision=? WHERE scope=? AND internal_id=?", (state["revision"], self.scope, internal))
        else:
            conn.execute("UPDATE SyncOrders SET authority_conflict_revision=NULL WHERE scope=? AND internal_id=?", (self.scope, internal))
        authority = encode({"facts": facts, "blocked": bool(blocked)})
        if authority != row["effect_authority"]:
            self._invalidate_effects(conn, internal)
            conn.execute("UPDATE SyncOrders SET effect_authority=? WHERE scope=? AND internal_id=?", (authority, self.scope, internal))
        if blocked:
            return error
        if state.get("deliveryStatus") not in ("IN_TRANSIT", "COMPLETED"):
            if old and old["facts"].get("deliveryStatus") in ("IN_TRANSIT", "COMPLETED"):
                return "Remote execution was undone/aborted; Farmax reversal is review-only"
            return None
        if not route or not route.get("id") or not route.get("startedAt") or not route.get("courierId"):
            return "Authoritative route/courier/time is missing"
        if state["deliveryStatus"] == "COMPLETED" and not route.get("completedAt"):
            return "Authoritative completion time is missing"
        generation = conn.execute("SELECT effect_generation FROM SyncOrders WHERE scope=? AND internal_id=?", (self.scope, internal)).fetchone()[0]
        order = json.loads(row["payload"] or restored_payload or "{}")
        mapping = conn.execute("SELECT local_id FROM DeliverymenMapping WHERE velide_id=?", (route["courierId"],)).fetchone()
        operations = ["IN_ROUTE"] + (["DONE"] if state["deliveryStatus"] == "COMPLETED" else [])
        for operation in operations:
            effect = {"execution_id": route["id"], "operation": operation, "started_at": route["startedAt"],
                      "completed_at": route.get("completedAt") if state["deliveryStatus"] == "COMPLETED" else None,
                      "occurred_at": route["startedAt"] if operation == "IN_ROUTE" else route["completedAt"],
                      "courier": route["courierId"], "courier_local_id": mapping[0] if mapping else None,
                      "source_context": order.get("source_context")}
            conn.execute("""INSERT INTO SyncEffects(scope,internal_id,effect_key,operation,payload,generation)
                VALUES(?,?,?,?,?,?) ON CONFLICT(scope,internal_id,effect_key) DO UPDATE SET
                payload=excluded.payload,generation=excluded.generation,state='PENDING',retry_at=0,last_error=NULL
                ,observation=NULL,observed_at=NULL
                WHERE SyncEffects.generation<>excluded.generation""",
                (self.scope, internal, route["id"]+":"+operation, operation, encode(effect), generation))
        return None

    def _completion_observed(self, conn, internal):
        return conn.execute("""SELECT 1 FROM SyncEffects E JOIN SyncOrders O ON O.scope=E.scope AND O.internal_id=E.internal_id
            WHERE E.scope=? AND E.internal_id=? AND E.operation='DONE' AND E.state='ACKNOWLEDGED'
            AND E.generation=O.effect_generation AND O.cancellation='NONE' AND O.source_review IS NULL""", (self.scope, internal)).fetchone() is not None

    def cancel(self, internal_id, external_id=None):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""INSERT OR IGNORE INTO SyncOrders(scope,internal_id,external_id,submission)
                VALUES(?,?,?,?)""", (self.scope, internal_id, external_id, "IMPORTED" if external_id else "UNKNOWN"))
            conn.execute("""UPDATE SyncOrders SET external_id=COALESCE(external_id,?),
                retry_at=CASE WHEN cancellation='NONE' THEN 0 ELSE retry_at END,
                add_attempts=CASE WHEN cancellation='NONE' AND external_id IS NULL AND request IS NOT NULL THEN 0 ELSE add_attempts END,
                cancellation=CASE WHEN cancellation='ACKNOWLEDGED' THEN cancellation
                WHEN submission='QUEUED' AND attempts=0 AND request IS NULL AND job_id IS NULL AND external_id IS NULL AND ? IS NULL
                    THEN 'ACKNOWLEDGED' ELSE 'REQUESTED' END
                WHERE scope=? AND internal_id=?""", (external_id, external_id, self.scope, internal_id))
            self._invalidate_effects(conn, internal_id)
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]

    def cancelled(self, internal_id):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""UPDATE SyncOrders SET cancellation='ACKNOWLEDGED', submission='TERMINAL',
                last_error=NULL WHERE scope=? AND internal_id=?""", (self.scope, internal_id))
            conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                         (DeliveryStatus.CANCELLED.value, internal_id))
            self._invalidate_effects(conn, internal_id)

    def failed(self, internal_id, operation, error, attempt=None, max_attempts=None, initial_delay=2, backoff_factor=2):
        with self._db() as db:
            conn = db._get_conn()
            row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            phase_attempt = row["delete_attempts" if operation == "DELETE" else "add_attempts"]
            exhausted = max_attempts is not None and phase_attempt >= max(1, max_attempts)
            delay = min(300, initial_delay * backoff_factor ** min(max(0, phase_attempt - 1), 20))
            result = conn.execute("""UPDATE SyncOrders SET last_error=?,retry_at=?,
                submission=CASE WHEN ?='ADD' THEN ? ELSE submission END,
                cancellation=CASE WHEN ? AND (?='DELETE' OR cancellation='REQUESTED') THEN 'CONFLICT' ELSE cancellation END
                WHERE scope=? AND internal_id=? AND submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED'
                AND (? IS NULL OR attempts=?) AND (?<>'ADD' OR (external_id IS NULL AND job_id IS NULL))""",
                (error, time.time()+delay, operation, "NEEDS_REVIEW" if exhausted else "RETRY", exhausted, operation,
                 self.scope, internal_id, attempt, attempt, operation))
            return result.rowcount == 1

    def defer_cancellation(self, internal_id, attempt):
        with self._db() as db:
            result = db._get_conn().execute("""UPDATE SyncOrders SET retry_at=?,
                last_error='Pedido cancelado no Farmax. Aguardando a entrega sair da rota ativa para cancelar no Velide.',
                delete_attempts=MAX(0,delete_attempts-1)
                WHERE scope=? AND internal_id=? AND attempts=? AND cancellation='REQUESTED'""",
                (time.time()+30, self.scope, internal_id, attempt))
            return result.rowcount == 1

    def observe_source(self, observations, cursor=None):
        with self._db() as db:
            conn = db._get_conn()
            for source_id, payload in observations:
                conn.execute("INSERT OR IGNORE INTO SyncSource(scope,source_id,payload) VALUES(?,?,?)",
                             (self.scope, source_id, encode(payload)))
            if cursor is not None:
                conn.execute("""INSERT INTO SyncCheckpoints(scope,cursor) VALUES(?,?)
                    ON CONFLICT(scope) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""", (self.scope, cursor))
        return observations

    def source_state(self):
        with self._db() as db:
            conn = db._get_conn()
            checkpoint = conn.execute("SELECT cursor FROM SyncCheckpoints WHERE scope=?", (self.scope,)).fetchone()
            pending = self._rows(conn, """SELECT * FROM SyncSource WHERE scope=? AND resolved=0
                AND source_id LIKE 'farmax:%' AND retry_at<=? ORDER BY attempts,rowid LIMIT 100""", (time.time(),))
            # Reserve a bounded retry turn, even if a worker crashes or the ERP
            # returns no detail rows. An unresolved prefix cannot starve later IDs.
            for row in pending:
                conn.execute("UPDATE SyncSource SET attempts=attempts+1,retry_at=?,last_error=COALESCE(last_error,?) WHERE scope=? AND source_id=?",
                             (time.time()+30, "Source detail has not been resolved", self.scope, row["source_id"]))
            return {"cursor": checkpoint[0] if checkpoint else None, "pending": pending}

    def resolve_source(self, source_id, error=None):
        with self._db() as db:
            db._get_conn().execute("UPDATE SyncSource SET resolved=?,last_error=? WHERE scope=? AND source_id=?",
                                  (0 if error else 1, error, self.scope, source_id))

    def enqueue_effect(self, internal_id, key, operation, payload, remote_state=None):
        with self._db() as db:
            conn = db._get_conn()
            if len(self.scope.split("|")) != 2:
                # Native writes still require versioned authoritative snapshots.
                return False
            rows = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))
            if not rows or rows[0]["cancellation"] != "NONE" or rows[0]["source_review"]:
                return False
            row = rows[0]
            previous = json.loads(row["effect_authority"] or "{}")
            old = previous.get("facts", {}).get("execution", {})
            value = dict(payload, operation=operation)
            execution = value.get("execution_id")
            if not execution or not value.get("occurred_at"):
                raise ValueError("Faltam os dados da rota para atualizar o Farmax.")
            value["started_at"] = value.get("started_at") or (value["occurred_at"] if operation == "IN_ROUTE" else old.get("startedAt") if old.get("id") == execution else None)
            value["completed_at"] = value.get("completed_at") or (value["occurred_at"] if operation == "DONE" else None)
            value["courier"] = value.get("courier") or (old.get("courierId") if old.get("id") == execution else None)
            if not value["started_at"] or not value["courier"]:
                raise ValueError("Aguardando os dados de saída e do entregador da rota.")
            if old.get("id") and old["id"] != execution:
                from datetime import datetime
                if datetime.fromisoformat(value["started_at"].replace("Z", "+00:00")) <= datetime.fromisoformat(old["startedAt"].replace("Z", "+00:00")):
                    return False
            if old.get("id") == execution and old.get("completedAt") and operation == "IN_ROUTE":
                return False
            route = {"id": execution, "startedAt": value["started_at"], "completedAt": value["completed_at"], "courierId": value["courier"]}
            authority = {"facts": {"deliveryStatus": "COMPLETED" if operation == "DONE" else "IN_TRANSIT", "execution": route}, "blocked": False}
            if authority != previous:
                self._invalidate_effects(conn, internal_id)
                conn.execute("UPDATE SyncOrders SET effect_authority=? WHERE scope=? AND internal_id=?", (encode(authority), self.scope, internal_id))
            generation = conn.execute("SELECT effect_generation FROM SyncOrders WHERE scope=? AND internal_id=?", (self.scope, internal_id)).fetchone()[0]
            mapping = conn.execute("SELECT local_id FROM DeliverymenMapping WHERE velide_id=?", (value["courier"],)).fetchone()
            value["courier_local_id"] = mapping[0] if mapping else None
            value["source_context"] = json.loads(row["payload"] or "{}").get("source_context") or value.get("source_context")
            conn.execute("""INSERT INTO SyncEffects(scope,internal_id,effect_key,operation,payload,generation) VALUES(?,?,?,?,?,?)
                ON CONFLICT(scope,internal_id,effect_key) DO UPDATE SET payload=excluded.payload,generation=excluded.generation,
                state='PENDING',retry_at=0,last_error=NULL WHERE SyncEffects.generation<>excluded.generation""",
                (self.scope, internal_id, key, operation, encode(value), generation))
            return True

    def next_effect(self):
        with self._db() as db:
            rows = self._rows(db._get_conn(), """SELECT E.* FROM SyncEffects E
                JOIN SyncOrders O ON O.scope=E.scope AND O.internal_id=E.internal_id
                WHERE E.scope=? AND E.state='PENDING' AND E.retry_at<=?
                AND E.generation=O.effect_generation AND O.effect_authority IS NOT NULL
                AND O.cancellation='NONE' AND O.source_review IS NULL
                AND NOT EXISTS (SELECT 1 FROM SyncEffects P WHERE P.scope=E.scope AND P.internal_id=E.internal_id
                    AND P.generation=E.generation AND P.operation='IN_ROUTE' AND P.state='PENDING' AND E.operation='DONE')
                ORDER BY E.sequence LIMIT 1""", (time.time(),))
            return rows[0] if rows else None

    def prepare_effect(self, sequence, generation):
        with self._db() as db:
            conn = db._get_conn()
            rows = self._rows(conn, """SELECT E.* FROM SyncEffects E JOIN SyncOrders O ON O.scope=E.scope AND O.internal_id=E.internal_id
                WHERE E.scope=? AND E.sequence=? AND E.generation=? AND E.generation=O.effect_generation
                AND E.state='PENDING' AND O.cancellation='NONE' AND O.source_review IS NULL AND O.effect_authority IS NOT NULL""", (sequence, generation))
            if not rows:
                return None
            effect = rows[0]
            payload = json.loads(effect["payload"])
            order = self._rows(conn, "SELECT payload,source_context,farmax_progress FROM SyncOrders WHERE scope=? AND internal_id=?", (effect["internal_id"],))[0]
            source = (json.loads(order["source_context"]) if order["source_context"] else
                      json.loads(order["farmax_progress"] or "{}").get("source_context") or
                      json.loads(order["payload"] or "{}").get("source_context"))
            if source:
                payload["source_context"] = source
            payload["legacy_adopted"] = self._legacy_adopted(conn)
            effect["payload"] = encode(payload)
            mapping = conn.execute("SELECT local_id FROM DeliverymenMapping WHERE velide_id=?", (payload["courier"],)).fetchone()
            if mapping and payload.get("courier_local_id") is None:
                payload["courier_local_id"] = normalize_farmax_id(mapping[0])
                effect["payload"] = encode(payload)
                conn.execute("UPDATE SyncEffects SET payload=? WHERE scope=? AND sequence=?", (effect["payload"], self.scope, sequence))
            if not mapping:
                raise FarmaxMappingMissing("O entregador ainda não está relacionado ao Farmax. Confira o vínculo de entregadores.")
            if normalize_farmax_id(payload.get("courier_local_id")) != normalize_farmax_id(mapping[0]):
                raise FarmaxEvidenceError("Historical courier mapping changed; review its provenance")
            return effect

    def effect_is_current(self, sequence, generation):
        with self._db() as db:
            return db._get_conn().execute("""SELECT 1 FROM SyncEffects E JOIN SyncOrders O
                ON O.scope=E.scope AND O.internal_id=E.internal_id
                WHERE E.scope=? AND E.sequence=? AND E.generation=? AND O.effect_generation=?
                AND E.state='PENDING' AND O.cancellation='NONE' AND O.source_review IS NULL""",
                (self.scope, sequence, generation, generation)).fetchone() is not None

    def apply_effect(self, sequence, generation, repository, timezone, current):
        with self._effect_lock:
            effect = self.prepare_effect(sequence, generation)
            if not effect or not current():
                return None
            payload = json.loads(effect["payload"])
            if not payload.get("source_context") and payload.get("legacy_adopted"):
                # Establish original-installation provenance BEFORE the writer.
                # A second source read under row locks must match this baseline.
                source = repository.fetch_deliveries_by_id((int(effect["internal_id"]),), pending_only=False)
                if len(source) != 1 or not source[0].source_context:
                    raise FarmaxEvidenceError("Não foi possível identificar a venda original no Farmax.")
                payload["source_context"] = source[0].source_context
                with self._db() as db:
                    db._get_conn().execute("UPDATE SyncOrders SET source_context=COALESCE(source_context,?) WHERE scope=? AND internal_id=?",
                                          (encode(payload["source_context"]), self.scope, effect["internal_id"]))
            check = lambda: current() and self.effect_is_current(sequence, generation)
            return repository.apply_delivery_effect(int(effect["internal_id"]), payload, timezone, authority_check=check)

    def effect_result(self, sequence, error=None, generation=None, observation=None, error_kind="TRANSIENT"):
        with self._db() as db:
            conn = db._get_conn()
            row = conn.execute("""SELECT E.internal_id,E.generation,O.effect_generation,O.cancellation,O.source_review,E.state,E.payload,E.operation,O.effect_authority
                FROM SyncEffects E JOIN SyncOrders O ON O.scope=E.scope AND O.internal_id=E.internal_id
                WHERE E.scope=? AND E.sequence=?""", (self.scope, sequence)).fetchone()
            if not row or generation is None or row[1] != generation or row[2] != generation or row[3] != "NONE" or row[4] is not None or row[5] != "PENDING":
                return False
            if error is not None and error_kind == "SOURCE_CONFLICT":
                self._latch_source_review(conn, row[0], {"kind": error_kind, "message": error, "effect_sequence": sequence})
                conn.execute("UPDATE SyncEffects SET last_error=? WHERE scope=? AND sequence=?", (error, self.scope, sequence))
                return True
            if error is None and observation not in ("COMPLETED_FIELDS_MATCH", "DEPARTURE_FIELDS_MATCH"):
                raise ValueError("A persisted-field observation is required for acknowledgement")
            if error is None:
                payload = json.loads(row[6])
                authority = json.loads(row[8] or "{}")
                if observation == "DEPARTURE_FIELDS_MATCH" and row[7] != "IN_ROUTE":
                    return False
                if observation == "COMPLETED_FIELDS_MATCH" and (not payload.get("completed_at") or authority.get("facts", {}).get("deliveryStatus") != "COMPLETED"):
                    return False
                mapping = conn.execute("SELECT local_id FROM DeliverymenMapping WHERE velide_id=?", (payload.get("courier"),)).fetchone()
                if not mapping or payload.get("courier_local_id") is None or normalize_farmax_id(mapping[0]) != normalize_farmax_id(payload["courier_local_id"]):
                    self._latch_source_review(conn,row[0],{"kind":"SOURCE_CONFLICT","message":"Courier mapping changed during observation"})
                    return False
            conn.execute("""UPDATE SyncEffects SET state=?,last_error=?, attempts=attempts+1,retry_at=?
                WHERE scope=? AND sequence=?""",
                         ("PENDING" if error else "ACKNOWLEDGED", error, time.time()+30 if error else 0, self.scope, sequence))
            if error is None:
                conn.execute("UPDATE SyncEffects SET observation=?,observed_at=CURRENT_TIMESTAMP WHERE scope=? AND sequence=?", (observation, self.scope, sequence))
                if observation == "DEPARTURE_FIELDS_MATCH":
                    conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?", (DeliveryStatus.IN_PROGRESS.value, row[0]))
                if observation == "COMPLETED_FIELDS_MATCH":
                    conn.execute("UPDATE SyncEffects SET state='ACKNOWLEDGED',last_error=NULL,observation='COMPLETED_FIELDS_MATCH',observed_at=CURRENT_TIMESTAMP WHERE scope=? AND internal_id=? AND generation=? AND state='PENDING'",
                                 (self.scope, row[0], generation))
                if self._completion_observed(conn, row[0]):
                    conn.execute("UPDATE SyncOrders SET submission='TERMINAL' WHERE scope=? AND internal_id=?",
                                 (self.scope, row[0]))
                    conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                                 (DeliveryStatus.DELIVERED.value, row[0]))
            return True

    def retry_work(self, internal_id):
        with self._db() as db:
            conn = db._get_conn()
            rows = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))
            if not rows:
                raise ValueError("Unknown local order")
            row = rows[0]
            if row["source_review"]:
                raise ValueError("Resolve the source review explicitly before retrying synchronization")
            if row["cancellation"] == "CONFLICT":
                conn.execute("UPDATE SyncOrders SET cancellation='REQUESTED',add_attempts=0,delete_attempts=0,retry_at=0 WHERE scope=? AND internal_id=?", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            elif row["external_id"]:
                conn.execute("UPDATE SyncEffects SET retry_at=0 WHERE scope=? AND internal_id=? AND state='PENDING'", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            elif row["job_id"]:
                row["retry_operation"] = "JOB"
            elif row["cancellation"] == "NONE":
                conn.execute("UPDATE SyncOrders SET submission='QUEUED',add_attempts=0,retry_at=0,last_error=NULL WHERE scope=? AND internal_id=?", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            else:
                raise ValueError("Cancellation is unresolved")
            return row

    def retry_is_current(self, internal, job_id, generation):
        with self._db() as db:
            rows = self._rows(db._get_conn(),"SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?",(internal,))
            if not rows:
                return False
            row = rows[0]
            return (row["job_id"] == job_id and row["effect_generation"] == generation and row["source_review"] is None
                    and row["cancellation"] == "NONE" and row["external_id"] is None and row["submission"] != "TERMINAL")
