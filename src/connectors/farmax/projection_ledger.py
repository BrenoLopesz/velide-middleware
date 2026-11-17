"""Integration-owned receipts and item write versions, in the ERP transaction.

Watching *every* write to a tracked item distinguishes a later native V -> V
return from our own completion. Matching a status alone is not provenance.
"""
import json
from threading import RLock

from sqlalchemy import BigInteger, Float, String, Text, inspect, text

from connectors.farmax.errors import FarmaxEvidenceError, FarmaxWaitingError
from connectors.farmax.farmax_setup import sql_tokens


TABLES = {
    "VELIDE_PROJECTION_APPLY": ({"APPLICATION_ID", "SOURCE_SCOPE", "SALE_ID", "GENERATION", "RECEIPT"}, {"APPLICATION_ID"}),
    "VELIDE_ITEM_GUARD": ({"CD_FILIAL", "CD_VENDA", "ID_PRODUTO", "ORDEM", "WRITE_VERSION"},
                          {"CD_FILIAL", "CD_VENDA", "ID_PRODUTO", "ORDEM"}),
}
_setup_lock = RLock()
KEYS = ("cd_filial", "cd_venda", "id_produto", "ordem")
WHERE = " AND ".join(name.upper() + "=:" + name for name in KEYS)
FIREBIRD_TRIGGER = """AS BEGIN
    UPDATE VELIDE_ITEM_GUARD SET WRITE_VERSION=WRITE_VERSION+1
    WHERE ((UPDATING OR DELETING) AND CD_FILIAL=OLD.CD_FILIAL AND CD_VENDA=OLD.CD_VENDA
        AND ID_PRODUTO=OLD.ID_PRODUTO AND ORDEM=OLD.ORDEM)
       OR ((INSERTING OR UPDATING) AND CD_FILIAL=NEW.CD_FILIAL AND CD_VENDA=NEW.CD_VENDA
        AND ID_PRODUTO=NEW.ID_PRODUTO AND ORDEM=NEW.ORDEM);
END"""


def encode(value):
    return json.dumps(value, sort_keys=True, default=str)


class ProjectionLedger:
    def __init__(self, engine):
        self.engine = engine

    def ensure(self, install=True):
        """Restartable DDL; never replace a conflicting installed object."""
        with _setup_lock:
            inspector = inspect(self.engine)
            for table, (columns, primary) in TABLES.items():
                if inspector.has_table(table):
                    actual = {column["name"].upper(): column for column in inspector.get_columns(table)}
                    if set(actual) != columns:
                        raise FarmaxEvidenceError("Incompatible projection ledger: " + table)
                    expected = ({"APPLICATION_ID": (String, 36), "SOURCE_SCOPE": (String, 64),
                                 "SALE_ID": (Float, None), "GENERATION": (BigInteger, None), "RECEIPT": (Text, None)}
                                if table == "VELIDE_PROJECTION_APPLY" else
                                dict({key.upper(): (Float, None) for key in KEYS}, WRITE_VERSION=(BigInteger, None)))
                    for name, (kind, length) in expected.items():
                        column = actual[name]
                        type_matches = isinstance(column["type"], kind) or (
                            kind is BigInteger and getattr(column["type"], "__visit_name__", "") == "BIGINT")
                        if (not type_matches
                                or (length is not None and getattr(column["type"], "length", None) != length)
                                or (column.get("nullable") and name not in primary)):
                            raise FarmaxEvidenceError("Incompatible projection ledger column: " + table + "." + name)
                    pk = inspector.get_pk_constraint(table).get("constrained_columns") or []
                    if {name.upper() for name in pk} != primary:
                        raise FarmaxEvidenceError("Projection ledger has no verified unique key: " + table)
                else:
                    if not install:
                        raise FarmaxWaitingError("Projection ledger has not been installed")
                    # Firebird requires each metadata dependency to be committed.
                    blob = "BLOB SUB_TYPE TEXT" if self.engine.dialect.name.startswith("firebird") else "TEXT"
                    definition = (
                        "APPLICATION_ID VARCHAR(36) NOT NULL PRIMARY KEY,SOURCE_SCOPE VARCHAR(64) NOT NULL,"
                        "SALE_ID DOUBLE PRECISION NOT NULL,GENERATION BIGINT NOT NULL,RECEIPT " + blob + " NOT NULL"
                        if table == "VELIDE_PROJECTION_APPLY" else
                        "CD_FILIAL DOUBLE PRECISION NOT NULL,CD_VENDA DOUBLE PRECISION NOT NULL,"
                        "ID_PRODUTO DOUBLE PRECISION NOT NULL,ORDEM DOUBLE PRECISION NOT NULL,"
                        "WRITE_VERSION BIGINT NOT NULL,PRIMARY KEY(CD_FILIAL,CD_VENDA,ID_PRODUTO,ORDEM)")
                    with self.engine.begin() as conn:
                        conn.execute(text("CREATE TABLE " + table + " (" + definition + ")"))
            if install and not any(index["name"].upper() == "VELIDE_PROJECTION_ORDER" for index in inspect(self.engine).get_indexes("VELIDE_PROJECTION_APPLY")):
                with self.engine.begin() as conn:
                    conn.execute(text("CREATE INDEX VELIDE_PROJECTION_ORDER ON VELIDE_PROJECTION_APPLY(SOURCE_SCOPE,SALE_ID)"))
            if self.engine.dialect.name.startswith("firebird"):
                with self.engine.connect() as conn:
                    row = conn.execute(text("""SELECT RDB$RELATION_NAME,RDB$TRIGGER_TYPE,
                        RDB$TRIGGER_SOURCE,COALESCE(RDB$TRIGGER_INACTIVE,0)
                        FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME='VELIDE_ITEM_VERSION'""")).fetchone()
                if row is None:
                    if not install:
                        raise FarmaxWaitingError("Item provenance trigger has not been installed")
                    with self.engine.begin() as conn:
                        conn.execute(text("CREATE TRIGGER VELIDE_ITEM_VERSION FOR VENDAS ACTIVE "
                                          "AFTER INSERT OR UPDATE OR DELETE POSITION 32767 " + FIREBIRD_TRIGGER))
                elif (row[0].strip() != "VENDAS" or row[1] != 114 or row[3] != 0
                      or sql_tokens(row[2]) != sql_tokens(FIREBIRD_TRIGGER)):
                    raise FarmaxEvidenceError("Item provenance trigger is incompatible or inactive")
            elif self.engine.dialect.name == "sqlite":
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    names = ("OLD", "NEW") if operation == "UPDATE" else (("NEW",) if operation == "INSERT" else ("OLD",))
                    predicate = " OR ".join("(" + " AND ".join(
                        key.upper() + "=" + alias + "." + key.upper() for key in KEYS) + ")" for alias in names)
                    name = "VELIDE_ITEM_VERSION_" + operation
                    statement = ("CREATE TRIGGER " + name + " AFTER " + operation +
                                 " ON VENDAS BEGIN UPDATE VELIDE_ITEM_GUARD SET WRITE_VERSION=WRITE_VERSION+1 WHERE " + predicate + "; END")
                    with self.engine.begin() as conn:
                        old = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"), {"name": name}).scalar()
                        if old is None:
                            if not install:
                                raise FarmaxWaitingError("Item provenance trigger has not been installed")
                            conn.execute(text(statement))
                        elif sql_tokens(old) != sql_tokens(statement):
                            raise FarmaxEvidenceError("Item provenance trigger is incompatible")
            else:
                raise FarmaxEvidenceError("Unsupported projection ledger dialect")

    def get(self, conn, application_id):
        row = conn.execute(text("SELECT RECEIPT FROM VELIDE_PROJECTION_APPLY WHERE APPLICATION_ID=:id"),
                           {"id": application_id}).scalar()
        return json.loads(row) if row else None

    def history(self, conn, scope, sale_id):
        # Per-source/order only. Receipts are immutable and retained for recovery.
        return [json.loads(row[0]) for row in conn.execute(text(
            "SELECT RECEIPT FROM VELIDE_PROJECTION_APPLY WHERE SOURCE_SCOPE=:scope AND SALE_ID=:sale"),
            {"scope": scope, "sale": sale_id})]

    def watch(self, conn, row):
        params = {key: row[key] for key in KEYS}
        version = conn.execute(text("SELECT WRITE_VERSION FROM VELIDE_ITEM_GUARD WHERE " + WHERE), params).scalar()
        if version is None:
            conn.execute(text("INSERT INTO VELIDE_ITEM_GUARD(CD_FILIAL,CD_VENDA,ID_PRODUTO,ORDEM,WRITE_VERSION) "
                              "VALUES(:cd_filial,:cd_venda,:id_produto,:ordem,0)"), params)
            version = 0
        return version

    def version(self, conn, row):
        return conn.execute(text("SELECT WRITE_VERSION FROM VELIDE_ITEM_GUARD WHERE " + WHERE),
                            {key: row[key] for key in KEYS}).scalar()

    def save(self, conn, application_id, scope, sale_id, generation, receipt):
        conn.execute(text("INSERT INTO VELIDE_PROJECTION_APPLY(APPLICATION_ID,SOURCE_SCOPE,SALE_ID,GENERATION,RECEIPT) "
                          "VALUES(:application,:scope,:sale,:generation,:receipt)"),
                     dict(application=application_id, scope=scope, sale=sale_id, generation=generation, receipt=encode(receipt)))
