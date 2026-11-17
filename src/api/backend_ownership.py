"""One durable owner per ERP reference, independent of API URL or login session.

The ledger commits before a backend journal. A crash between those commits leaves
an intake receipt for that owner, never permission to resend through another API.
Ownership transfer is only an offline, authoritative migration operation.
"""
from contextlib import contextmanager
import json
import sqlite3
import uuid


class OwnershipError(ValueError):
    pass


class BackendOwnership:
    def __init__(self, path, source_identity):
        self.path, self.source_identity = str(path), source_identity
        with self._db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise OwnershipError("A versão do registro de propriedade não é compatível.")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"Installation", "Profiles", "Owners"}
            if tables - expected or version == 1 and tables != expected:
                raise OwnershipError("O arquivo informado não é um registro de propriedade de conexões.")
            conn.execute("CREATE TABLE IF NOT EXISTS Installation(id INTEGER PRIMARY KEY CHECK(id=1),source TEXT NOT NULL,active TEXT,identity TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO Installation(id,source,identity) VALUES(1,?,?)", (source_identity, str(uuid.uuid4())))
            source, self.identity = conn.execute("SELECT source,identity FROM Installation WHERE id=1").fetchone()
            if source != source_identity:
                raise OwnershipError("O registro de propriedade pertence a outra origem de pedidos.")
            conn.execute("""CREATE TABLE IF NOT EXISTS Profiles(
                name TEXT PRIMARY KEY,scope TEXT NOT NULL UNIQUE,principal TEXT,unverified_legacy INTEGER NOT NULL DEFAULT 0,
                adopted INTEGER NOT NULL DEFAULT 0,database_path TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS Owners(
                internal_id TEXT PRIMARY KEY,profile TEXT NOT NULL REFERENCES Profiles(name),
                payload TEXT,source_file TEXT,recorded INTEGER NOT NULL DEFAULT 0,conflict TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS owners_profile_receipts ON Owners(profile,recorded)")
            conn.execute("PRAGMA user_version=1")

    def fork_identity(self):
        """Give a private cutover copy its own config binding before publication."""
        identity = str(uuid.uuid4())
        with self._db() as conn:
            conn.execute("UPDATE Installation SET identity=? WHERE id=1", (identity,))
        self.identity = identity

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register(self, name, scope, database_path=None):
        if name not in ("legacy", "native"):
            raise OwnershipError("Perfil de conexão inválido.")
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO Profiles(name,scope,database_path) VALUES(?,?,?)", (name, scope, database_path))
            row = conn.execute("SELECT scope,database_path FROM Profiles WHERE name=?", (name,)).fetchone()
            if not row or row[0] != scope or (row[1] is not None and row[1] != database_path):
                raise OwnershipError("A conexão de um perfil com pedidos retidos não pode ser substituída.")

    def select(self, name, publish=None):
        with self._db() as conn:
            if not conn.execute("SELECT 1 FROM Profiles WHERE name=?", (name,)).fetchone():
                raise OwnershipError("A conexão solicitada ainda não foi provisionada.")
            # Publishing config while holding the ledger's intake lock makes the
            # selector one boundary. Startup repairs a crash after file replace.
            if publish is not None:
                publish()
            conn.execute("UPDATE Installation SET active=? WHERE id=1", (name,))

    def selected(self):
        with self._db() as conn:
            return conn.execute("SELECT active FROM Installation WHERE id=1").fetchone()[0]

    def seed(self, name, internal_ids, principal=None):
        """Adopt exact retained references, without stealing another owner's work."""
        internal_ids = list(internal_ids)
        with self._db() as conn:
            for internal in internal_ids:
                conn.execute("INSERT OR IGNORE INTO Owners(internal_id,profile,recorded) VALUES(?,?,1)", (internal, name))
            if name == "legacy":
                conn.execute("UPDATE Profiles SET principal=COALESCE(principal,?),unverified_legacy=CASE WHEN COALESCE(principal,?) IS NULL AND ? THEN 1 ELSE 0 END,adopted=1 WHERE name=? AND adopted=0",
                             (principal, principal, bool(internal_ids), name))

    def bind_principal(self, name, principal):
        """Called only after a successful authenticated API scope/read check."""
        if not principal:
            raise OwnershipError("Não foi possível identificar a conta autenticada.")
        with self._db() as conn:
            old, unverified = conn.execute("SELECT principal,unverified_legacy FROM Profiles WHERE name=?", (name,)).fetchone()
            if name == "legacy" and (unverified or (old and old != principal)):
                raise OwnershipError("Esta conta não pode assumir os pedidos da conexão legada. Entre com a conta original ou solicite revisão ao suporte.")
            # Native merchant/connection binding is checked by the API. A tenant
            # migration may legitimately give its authorized user another sub.
            conn.execute("UPDATE Profiles SET principal=? WHERE name=?", (principal, name))

    def check_principal(self, name, principal):
        """Continuity check before token storage/UI publication; grants no access."""
        with self._db() as conn:
            old, unverified = conn.execute("SELECT principal,unverified_legacy FROM Profiles WHERE name=?", (name,)).fetchone()
            if name == "legacy" and (unverified or old and old != principal):
                raise OwnershipError("Esta conta não pode assumir os pedidos da conexão legada. Entre com a conta original ou solicite revisão ao suporte.")

    def intake(self, internal, payload, source_file=None):
        encoded = json.dumps({k: v for k, v in payload.items() if v is not None}, sort_keys=True, ensure_ascii=False)
        with self._db() as conn:
            selected = conn.execute("SELECT active FROM Installation WHERE id=1").fetchone()[0]
            if selected is None:
                raise OwnershipError("Selecione uma conexão provisionada antes de receber pedidos.")
            conn.execute("INSERT OR IGNORE INTO Owners(internal_id,profile,payload,source_file) VALUES(?,?,?,?)",
                         (internal, selected, encoded, source_file))
            owner, original = conn.execute("SELECT profile,payload FROM Owners WHERE internal_id=?", (internal,)).fetchone()
            if original is None:
                conn.execute("UPDATE Owners SET payload=?,source_file=?,recorded=0 WHERE internal_id=?", (encoded, source_file, internal))
            elif original != encoded:
                conn.execute("UPDATE Owners SET conflict=?,recorded=0 WHERE internal_id=?",
                             ("Os dados originais do pedido foram alterados. Confira a origem antes de reenviar.", internal))
            return owner

    def owner(self, internal):
        with self._db() as conn:
            row = conn.execute("SELECT profile FROM Owners WHERE internal_id=?", (internal,)).fetchone()
            return row[0] if row else None

    def adopt_remote(self, name, internal):
        """A source-authorized server lookup can recover an erased local receipt."""
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO Owners(internal_id,profile,recorded) VALUES(?,?,1)", (internal, name))
            return conn.execute("SELECT profile FROM Owners WHERE internal_id=?", (internal,)).fetchone()[0] == name

    def can_execute(self, name, internal):
        with self._db() as conn:
            row = conn.execute("SELECT profile,conflict FROM Owners WHERE internal_id=?", (internal,)).fetchone()
            return bool(row and row[0] == name and not row[1])

    def owned_ids(self, name, executable=False):
        with self._db() as conn:
            statement = "SELECT internal_id FROM Owners WHERE profile=?" + (" AND conflict IS NULL" if executable else "")
            return {row[0] for row in conn.execute(statement, (name,))}

    def recorded_ids(self, name):
        with self._db() as conn:
            return {row[0] for row in conn.execute("SELECT internal_id FROM Owners WHERE profile=? AND recorded=1", (name,))}

    def receipts(self, name):
        with self._db() as conn:
            return [dict(internal_id=row[0], payload=json.loads(row[1]), source_file=row[2], conflict=row[3])
                    for row in conn.execute("SELECT internal_id,payload,source_file,conflict FROM Owners WHERE profile=? AND recorded=0 AND payload IS NOT NULL", (name,))]

    def recorded(self, name, internal, conflict=None):
        with self._db() as conn:
            # A scanner may latch a conflict after the owner-journal commit.
            # Only acknowledge the conflict snapshot already presented there.
            conn.execute("UPDATE Owners SET recorded=1 WHERE internal_id=? AND profile=? AND conflict IS ?",
                         (internal, name, conflict))

    def transfer(self, internal_ids, previous, destination):
        """Offline cutover only; caller publishes a private ledger copy with config."""
        with self._db() as conn:
            for internal in internal_ids:
                row = conn.execute("SELECT profile FROM Owners WHERE internal_id=?", (internal,)).fetchone()
                if row and row[0] not in (previous, destination):
                    raise OwnershipError("O pacote contradiz a propriedade retida do pedido.")
                conn.execute("INSERT OR IGNORE INTO Owners(internal_id,profile,recorded) VALUES(?,?,1)", (internal, destination))
                conn.execute("UPDATE Owners SET profile=?,payload=NULL,recorded=1 WHERE internal_id=?", (destination, internal))


class ProfileOwnership:
    def __init__(self, registry, name):
        self.registry, self.name = registry, name

    def owns(self, internal):
        return self.registry.owner(internal) == self.name

    def executable(self, internal):
        return self.registry.can_execute(self.name, internal)
