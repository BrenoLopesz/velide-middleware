"""Transactional obligations shared by source ingestion, dispatch and ERP effects."""
import json
import time
from contextlib import contextmanager

from api.sqlite_manager import SQLiteManager, DeliveryStatus
from connectors.farmax.identity import normalize_farmax_id


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class SyncJournal:
    def __init__(self, path, scope, source_identity=None):
        self.path, self.scope = path, scope
        self.source_identity = source_identity

    @contextmanager
    def _db(self):
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

    def accept(self, internal_id, payload):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""INSERT OR IGNORE INTO SyncOrders(scope,internal_id,payload)
                VALUES(?,?,?)""", (self.scope, internal_id, encode(payload)))
            # Restoration may supply the missing source snapshot, but never change
            # an already accepted payload or reset a tombstone.
            conn.execute("""UPDATE SyncOrders SET payload=? WHERE scope=? AND internal_id=?
                AND payload IS NULL""", (encode(payload), self.scope, internal_id))
            row = self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            before = {key: value for key, value in json.loads(row["payload"]).items() if value is not None}
            after = {key: value for key, value in payload.items() if value is not None}
            if before != after:
                raise ValueError("Source payload conflicts with an already accepted order")
            return row

    def active_orders(self):
        with self._db() as db:
            # Linked terminal deliveries remain observable: native undo/redo can
            # change them after an ERP match was acknowledged.
            return self._rows(db._get_conn(), """SELECT internal_id FROM SyncOrders WHERE scope=?
                AND (external_id IS NOT NULL OR (submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED')) ORDER BY internal_id""")

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
                AND ((cancellation='REQUESTED' AND (external_id IS NOT NULL OR ?))
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
            return row

    def dispatch_is_current(self, internal, attempt, operation):
        with self._db() as db:
            rows = self._rows(db._get_conn(),"SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?",(internal,))
            if not rows or rows[0]["attempts"] != attempt:
                return False
            row = rows[0]
            if operation == "DELETE":
                return row["cancellation"] == "REQUESTED"
            return row["submission"] == "SUBMITTING" and row["cancellation"] == "NONE" and row["source_review"] is None and row["external_id"] is None and row["job_id"] is None

    def imported(self, internal_id, external_id):
        with self._db() as db:
            conn = db._get_conn()
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
                cancellation=CASE WHEN ?='DELETE' THEN 'CONFLICT' ELSE cancellation END
                WHERE scope=? AND internal_id=? AND submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED'
                AND (? IS NULL OR attempts=?) AND (?<>'ADD' OR (external_id IS NULL AND job_id IS NULL))""",
                (error, operation, operation, self.scope, internal_id, attempt, attempt, operation))
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
                    payload = encode({"internal_id": internal, "customerName": source["customerName"],
                        "customerContact": source.get("customerContact"), "address": request["rawAddress"],
                        "address2": request.get("rawComplemento"), "reference": request.get("rawReference"),
                        "neighbourhood": request.get("rawNeighbourhood"), "createdAt": request["createdAtIntent"],
                        "source_context": source.get("sourceContext")})
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
        error = "Source/cancellation conflict requires review" if blocked else None
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
                cancellation=CASE WHEN cancellation='ACKNOWLEDGED' THEN cancellation
                WHEN submission='QUEUED' AND external_id IS NULL AND ? IS NULL
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

    def failed(self, internal_id, operation, error, attempt=None):
        with self._db() as db:
            conn = db._get_conn()
            row = self._rows(conn, "SELECT attempts FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            delay = min(300, 2 ** min(row["attempts"], 8))
            result = conn.execute("""UPDATE SyncOrders SET last_error=?,retry_at=?,
                submission=CASE WHEN ?='ADD' THEN 'RETRY' ELSE submission END
                WHERE scope=? AND internal_id=? AND submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED'
                AND (? IS NULL OR attempts=?) AND (?<>'ADD' OR (external_id IS NULL AND job_id IS NULL))""",
                (error, time.time()+delay, operation, self.scope, internal_id, attempt, attempt, operation))
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
            # Legacy action streams have no authoritative execution revision.
            # They may request a refresh, but never authorize an ERP obligation.
            self._invalidate_effects(conn, internal_id)
            conn.execute("UPDATE SyncOrders SET last_error=? WHERE scope=? AND internal_id=?",
                         ("Unversioned event requires authoritative reconciliation", self.scope, internal_id))

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
            mapping = conn.execute("SELECT local_id FROM DeliverymenMapping WHERE velide_id=?", (payload["courier"],)).fetchone()
            if mapping and payload.get("courier_local_id") is None:
                payload["courier_local_id"] = normalize_farmax_id(mapping[0])
                effect["payload"] = encode(payload)
                conn.execute("UPDATE SyncEffects SET payload=? WHERE scope=? AND sequence=?", (effect["payload"], self.scope, sequence))
            if not mapping or normalize_farmax_id(payload.get("courier_local_id")) != normalize_farmax_id(mapping[0]):
                raise ValueError("Historical courier mapping is missing or changed; verify it before recognizing source fields")
            return effect

    def effect_result(self, sequence, error=None, generation=None, observation=None):
        with self._db() as db:
            conn = db._get_conn()
            row = conn.execute("""SELECT E.internal_id,E.generation,O.effect_generation,O.cancellation,O.source_review,E.state,E.payload,E.operation,O.effect_authority
                FROM SyncEffects E JOIN SyncOrders O ON O.scope=E.scope AND O.internal_id=E.internal_id
                WHERE E.scope=? AND E.sequence=?""", (self.scope, sequence)).fetchone()
            if not row or generation is None or row[1] != generation or row[2] != generation or row[3] != "NONE" or row[4] is not None or row[5] != "PENDING":
                return False
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
                    conn.execute("UPDATE SyncEffects SET last_error=?,retry_at=? WHERE scope=? AND sequence=?", ("Courier mapping changed during observation", time.time()+30, self.scope, sequence))
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
            if row["cancellation"] == "CONFLICT":
                conn.execute("UPDATE SyncOrders SET cancellation='REQUESTED',retry_at=0 WHERE scope=? AND internal_id=?", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            elif row["external_id"]:
                conn.execute("UPDATE SyncEffects SET retry_at=0 WHERE scope=? AND internal_id=? AND state='PENDING'", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            elif row["job_id"]:
                row["retry_operation"] = "JOB"
            elif row["cancellation"] == "NONE":
                conn.execute("UPDATE SyncOrders SET submission='QUEUED',retry_at=0,last_error=NULL WHERE scope=? AND internal_id=?", (self.scope, internal_id))
                row["retry_operation"] = "LOCAL"
            else:
                raise ValueError("Cancellation is unresolved")
            return row
