"""Transactional obligations shared by source ingestion, dispatch and ERP effects."""
import json
import time

from api.sqlite_manager import SQLiteManager, DeliveryStatus


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class SyncJournal:
    def __init__(self, path, scope):
        self.path, self.scope = path, scope

    def _rows(self, conn, sql, args=()):
        cursor = conn.execute(sql, (self.scope,) + tuple(args))
        names = [field[0] for field in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def restore(self):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            # Preserve pre-upgrade identities, including unresolved MISSING/FAILED.
            for external, internal, status in db.get_all_deliveries():
                state = "TERMINAL" if status in (DeliveryStatus.CANCELLED, DeliveryStatus.DELIVERED) else "IMPORTED"
                conn.execute("""INSERT OR IGNORE INTO SyncOrders
                    (scope, internal_id, external_id, submission) VALUES (?, ?, ?, ?)""",
                             (self.scope, internal, external, state))
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=?")

    def accept(self, internal_id, payload):
        with SQLiteManager(self.path) as db:
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

    def freeze_request(self, internal_id, request):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("UPDATE SyncOrders SET request=? WHERE scope=? AND internal_id=? AND request IS NULL",
                         (encode(request), self.scope, internal_id))
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]

    def claim(self):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            rows = self._rows(conn, """SELECT * FROM SyncOrders WHERE scope=? AND retry_at<=?
                AND cancellation<>'ACKNOWLEDGED' AND submission<>'TERMINAL'
                AND ((cancellation='REQUESTED' AND external_id IS NOT NULL)
                     OR (submission IN ('QUEUED','SUBMITTING','RETRY') AND payload IS NOT NULL))
                ORDER BY rowid LIMIT 1""", (time.time(),))
            if not rows:
                return None
            row = rows[0]
            row["operation"] = "DELETE" if row["cancellation"] == "REQUESTED" and row["external_id"] else "ADD"
            conn.execute("""UPDATE SyncOrders SET attempts=attempts+1,
                submission=CASE WHEN ?='ADD' THEN 'SUBMITTING' ELSE submission END
                WHERE scope=? AND internal_id=?""", (row["operation"], self.scope, row["internal_id"]))
            return row

    def imported(self, internal_id, external_id):
        with SQLiteManager(self.path) as db:
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

    def cancel(self, internal_id, external_id=None):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT OR IGNORE INTO SyncOrders(scope,internal_id,external_id,submission)
                VALUES(?,?,?,?)""", (self.scope, internal_id, external_id, "IMPORTED" if external_id else "UNKNOWN"))
            conn.execute("""UPDATE SyncOrders SET external_id=COALESCE(external_id,?),
                cancellation=CASE WHEN cancellation='ACKNOWLEDGED' THEN cancellation
                WHEN submission='QUEUED' AND external_id IS NULL AND ? IS NULL
                    THEN 'ACKNOWLEDGED' ELSE 'REQUESTED' END
                WHERE scope=? AND internal_id=?""", (external_id, external_id, self.scope, internal_id))
            return self._rows(conn, "SELECT * FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]

    def cancelled(self, internal_id):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("""UPDATE SyncOrders SET cancellation='ACKNOWLEDGED', submission='TERMINAL',
                last_error=NULL WHERE scope=? AND internal_id=?""", (self.scope, internal_id))
            conn.execute("UPDATE DeliveryMapping SET status=? WHERE internal_delivery_id=?",
                         (DeliveryStatus.CANCELLED.value, internal_id))

    def failed(self, internal_id, operation, error):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            row = self._rows(conn, "SELECT attempts FROM SyncOrders WHERE scope=? AND internal_id=?", (internal_id,))[0]
            delay = min(300, 2 ** min(row["attempts"], 8))
            conn.execute("""UPDATE SyncOrders SET last_error=?,retry_at=?,
                submission=CASE WHEN ?='ADD' THEN 'RETRY' ELSE submission END
                WHERE scope=? AND internal_id=?""", (error, time.time()+delay, operation, self.scope, internal_id))

    def observe_source(self, observations, cursor=None):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            for source_id, payload in observations:
                conn.execute("INSERT OR IGNORE INTO SyncSource(scope,source_id,payload) VALUES(?,?,?)",
                             (self.scope, source_id, encode(payload)))
            if cursor is not None:
                conn.execute("""INSERT INTO SyncCheckpoints(scope,cursor) VALUES(?,?)
                    ON CONFLICT(scope) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)""", (self.scope, cursor))
        return observations

    def source_state(self):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            checkpoint = conn.execute("SELECT cursor FROM SyncCheckpoints WHERE scope=?", (self.scope,)).fetchone()
            pending = self._rows(conn, "SELECT * FROM SyncSource WHERE scope=? AND resolved=0 AND source_id LIKE 'farmax:%' ORDER BY rowid LIMIT 100")
            return {"cursor": checkpoint[0] if checkpoint else None, "pending": pending}

    def resolve_source(self, source_id, error=None):
        with SQLiteManager(self.path) as db:
            db._get_conn().execute("UPDATE SyncSource SET resolved=?,last_error=? WHERE scope=? AND source_id=?",
                                  (0 if error else 1, error, self.scope, source_id))

    def enqueue_effect(self, internal_id, key, operation, payload, remote_state=None):
        with SQLiteManager(self.path) as db:
            conn = db._get_conn()
            conn.execute("""INSERT OR IGNORE INTO SyncEffects(scope,internal_id,effect_key,operation,payload)
                VALUES(?,?,?,?,?)""", (self.scope, internal_id, key, operation, encode(payload)))
            if remote_state is not None:
                conn.execute("UPDATE SyncOrders SET remote_state=? WHERE scope=? AND internal_id=?",
                             (encode(remote_state), self.scope, internal_id))

    def next_effect(self):
        with SQLiteManager(self.path) as db:
            rows = self._rows(db._get_conn(), """SELECT E.* FROM SyncEffects E
                WHERE E.scope=? AND E.state='PENDING' AND E.retry_at<=?
                AND NOT EXISTS (SELECT 1 FROM SyncEffects P WHERE P.scope=E.scope
                    AND P.internal_id=E.internal_id AND P.state='PENDING' AND P.sequence<E.sequence)
                ORDER BY E.sequence LIMIT 1""", (time.time(),))
            return rows[0] if rows else None

    def effect_result(self, sequence, error=None):
        with SQLiteManager(self.path) as db:
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
