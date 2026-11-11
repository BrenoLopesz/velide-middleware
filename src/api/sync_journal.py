"""Transactional obligations shared by source ingestion, dispatch and ERP effects."""
import json
import time
from contextlib import contextmanager

from api.sqlite_manager import SQLiteManager, DeliveryStatus


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class SyncJournal:
    def __init__(self, path, scope):
        self.path, self.scope = path, scope

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
            yield db

    def _rows(self, conn, sql, args=()):
        cursor = conn.execute(sql, (self.scope,) + tuple(args))
        names = [field[0] for field in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

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
            if row["payload"] != encode(payload):
                raise ValueError("Source payload conflicts with an already accepted order")
            return row

    def active_orders(self):
        with self._db() as db:
            return self._rows(db._get_conn(), """SELECT internal_id FROM SyncOrders WHERE scope=?
                AND submission<>'TERMINAL' AND cancellation<>'ACKNOWLEDGED' ORDER BY internal_id""")

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
                     OR (submission IN ('QUEUED','SUBMITTING','RETRY') AND payload IS NOT NULL))
                ORDER BY rowid LIMIT 1""", (time.time(), native))
            if not rows:
                return None
            row = rows[0]
            row["operation"] = "DELETE" if row["cancellation"] == "REQUESTED" and (row["external_id"] or native) else "ADD"
            conn.execute("""UPDATE SyncOrders SET attempts=attempts+1,
                submission=CASE WHEN ?='ADD' THEN 'SUBMITTING' ELSE submission END
                WHERE scope=? AND internal_id=?""", (row["operation"], self.scope, row["internal_id"]))
            return row

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

    def accepted_job(self, internal_id, job_id):
        with self._db() as db:
            db._get_conn().execute("""UPDATE SyncOrders SET job_id=?,submission='ACCEPTED',last_error=NULL,retry_at=0
                WHERE scope=? AND internal_id=?""", (job_id, self.scope, internal_id))

    def needs_review(self, internal_id, operation, error):
        with self._db() as db:
            db._get_conn().execute("""UPDATE SyncOrders SET last_error=?,
                submission=CASE WHEN ?='ADD' THEN 'NEEDS_REVIEW' ELSE submission END,
                cancellation=CASE WHEN ?='DELETE' THEN 'CONFLICT' ELSE cancellation END
                WHERE scope=? AND internal_id=?""", (error, operation, operation, self.scope, internal_id))

    def native_states(self, states, farmax=True):
        """Commit accepted remote facts and their ERP obligations in one transaction."""
        with self._db() as db:
            conn = db._get_conn()
            for state in states:
                internal = state["externalOrderId"]
                if not state["found"]:
                    conn.execute("UPDATE SyncOrders SET last_error='Remote reference unresolved' WHERE scope=? AND internal_id=?",
                                 (self.scope, internal))
                    continue
                source, request = state.get("source"), state.get("request")
                payload = None
                if source and request:
                    payload = encode({"internal_id": internal, "customerName": source["customerName"],
                        "customerContact": source.get("customerContact"), "address": request["rawAddress"],
                        "address2": request.get("rawComplemento"), "reference": request.get("rawReference"),
                        "neighbourhood": request.get("rawNeighbourhood"), "createdAt": request["createdAtIntent"]})
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
                if state["cancellation"] == "ACKNOWLEDGED" or state.get("deliveryStatus") == "CANCELLED" or state["deleted"]:
                    submission, cancellation = "TERMINAL", "ACKNOWLEDGED"
                if external:
                    existing = db.get_delivery_by_internal_id(internal)
                    if existing and existing[0] != external:
                        raise ValueError("Migrate legacy delivery mappings before enabling native synchronization")
                    if not existing and not db.add_delivery_mapping(external, internal, DeliveryStatus.ADDED):
                        raise RuntimeError("Could not save native mapping")
                if state.get("deliveryStatus") in ("IN_TRANSIT", "COMPLETED") and farmax:
                    route = state.get("execution")
                    if not route or not route.get("startedAt") or not route.get("courierId"):
                        error = "Authoritative route/courier/time is missing"
                    else:
                        operations = [("IN_ROUTE", route["startedAt"])]
                        if state["deliveryStatus"] == "COMPLETED":
                            if route.get("completedAt"):
                                operations.append(("DONE", route["completedAt"]))
                            else:
                                error = "Authoritative completion time is missing"
                        for operation, occurred in operations:
                            conn.execute("""INSERT OR IGNORE INTO SyncEffects(scope,internal_id,effect_key,operation,payload)
                                VALUES(?,?,?,?,?)""", (self.scope, internal, route["id"]+":"+operation, operation,
                                encode({"execution_id": route["id"], "occurred_at": occurred, "courier": route["courierId"]})))
                elif state.get("deliveryStatus") == "COMPLETED":
                    submission = "TERMINAL"
                # Already acknowledged ERP work must not be reactivated by polling.
                if row["submission"] == "TERMINAL":
                    submission = "TERMINAL"
                conn.execute("""UPDATE SyncOrders SET submission=?,cancellation=?,external_id=COALESCE(?,external_id),
                    job_id=COALESCE(?,job_id),payload=COALESCE(payload,?),remote_state=?,last_error=? WHERE scope=? AND internal_id=?""",
                    (submission, cancellation, external, state.get("jobId"), payload, encode(state), error, self.scope, internal))
                if cancellation == "ACKNOWLEDGED":
                    conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                                 (DeliveryStatus.CANCELLED.value, internal))
            references = [state["externalOrderId"] for state in states]
            if not references:
                return []
            placeholders = ",".join("?" for _ in references)
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id IN (" + placeholders + ")", references)

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
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]

    def cancelled(self, internal_id):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""UPDATE SyncOrders SET cancellation='ACKNOWLEDGED', submission='TERMINAL',
                last_error=NULL WHERE scope=? AND internal_id=?""", (self.scope, internal_id))
            conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                         (DeliveryStatus.CANCELLED.value, internal_id))

    def failed(self, internal_id, operation, error):
        with self._db() as db:
            conn = db._get_conn()
            row = self._rows(conn, "SELECT attempts FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            delay = min(300, 2 ** min(row["attempts"], 8))
            conn.execute("""UPDATE SyncOrders SET last_error=?,retry_at=?,
                submission=CASE WHEN ?='ADD' THEN 'RETRY' ELSE submission END
                WHERE scope=? AND internal_id=?""", (error, time.time()+delay, operation, self.scope, internal_id))

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
            pending = self._rows(conn, "SELECT * FROM SyncSource WHERE scope=? AND resolved=0 AND source_id LIKE 'farmax:%' ORDER BY rowid LIMIT 100")
            return {"cursor": checkpoint[0] if checkpoint else None, "pending": pending}

    def resolve_source(self, source_id, error=None):
        with self._db() as db:
            db._get_conn().execute("UPDATE SyncSource SET resolved=?,last_error=? WHERE scope=? AND source_id=?",
                                  (0 if error else 1, error, self.scope, source_id))

    def enqueue_effect(self, internal_id, key, operation, payload, remote_state=None):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""INSERT OR IGNORE INTO SyncEffects(scope,internal_id,effect_key,operation,payload)
                VALUES(?,?,?,?,?)""", (self.scope, internal_id, key, operation, encode(payload)))
            if remote_state is not None:
                conn.execute("UPDATE SyncOrders SET remote_state=? WHERE scope=? AND internal_id=?",
                             (encode(remote_state), self.scope, internal_id))

    def next_effect(self):
        with self._db() as db:
            rows = self._rows(db._get_conn(), """SELECT E.* FROM SyncEffects E
                WHERE E.scope=? AND E.state='PENDING' AND E.retry_at<=?
                AND NOT EXISTS (SELECT 1 FROM SyncEffects P WHERE P.scope=E.scope
                    AND P.internal_id=E.internal_id AND P.state='PENDING' AND P.sequence<>E.sequence
                    AND ((P.sequence<E.sequence AND NOT (E.operation='IN_ROUTE' AND P.operation='DONE'
                        AND substr(E.effect_key,1,length(E.effect_key)-9)=substr(P.effect_key,1,length(P.effect_key)-5)))
                        OR (E.operation='DONE' AND P.operation='IN_ROUTE'
                        AND substr(E.effect_key,1,length(E.effect_key)-5)=substr(P.effect_key,1,length(P.effect_key)-9))))
                ORDER BY E.sequence LIMIT 1""", (time.time(),))
            return rows[0] if rows else None

    def effect_result(self, sequence, error=None):
        with self._db() as db:
            conn = db._get_conn()
            conn.execute("""UPDATE SyncEffects SET state=?,last_error=?, attempts=attempts+1,retry_at=?
                WHERE scope=? AND sequence=?""",
                         ("PENDING" if error else "ACKNOWLEDGED", error, time.time()+30 if error else 0, self.scope, sequence))
            if error is None:
                row = self._rows(conn, "SELECT * FROM SyncEffects WHERE scope=? AND sequence=?", (sequence,))[0]
                if row["operation"] == "DONE":
                    conn.execute("UPDATE SyncOrders SET submission='TERMINAL' WHERE scope=? AND internal_id=?",
                                 (self.scope, row["internal_id"]))
                    conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                                 (DeliveryStatus.DELIVERED.value, row["internal_id"]))

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
