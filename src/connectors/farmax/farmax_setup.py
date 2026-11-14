"""Install missing Middleware capture objects; preserve every existing object."""
import logging
import re
from sqlalchemy import text


def sql_tokens(source):
    # Whitespace/comments may vary, but preserve quoted literals (' ' != '').
    tokens = re.findall(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/|[A-Za-z_$][\w$]*|\d+|[^\s]", source or "")
    return tuple(token if token.startswith("'") else token.upper() for token in tokens
                 if not token.startswith(("--", "/*")))


class FarmaxSetup:
    SEQUENCE_NAME = "DELIVERYLOG_ID_AUTOINCREMENT"
    LOG_TABLE_NAME = "DELIVERYLOG"
    INCREMENT_TRIGGER_NAME = "TRG_DELIVERY_LOGID_INCREMENT"
    ADD_DELIVERY_TRIGGER_NAME = "TRG_ADD_DELIVERY"
    EXPECTED = {
        INCREMENT_TRIGGER_NAME: (LOG_TABLE_NAME, 1, "AS BEGIN NEW.ID = NEXT VALUE FOR DELIVERYLOG_ID_AUTOINCREMENT; END"),
        ADD_DELIVERY_TRIGGER_NAME: ("ENTREGAS", 114, """AS BEGIN
            IF (INSERTING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'INSERT',CURRENT_TIMESTAMP);
            ELSE IF (UPDATING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(NEW.CD_VENDA,'UPDATE',CURRENT_TIMESTAMP);
            ELSE IF (DELETING) THEN INSERT INTO DELIVERYLOG(CD_VENDA,ACTION,LOGDATE) VALUES(OLD.CD_VENDA,'DELETE',CURRENT_TIMESTAMP);
        END"""),
    }

    def __init__(self, engine):
        self._engine = engine
        self.logger = logging.getLogger(__name__)

    def initial_setup(self):
        """Create missing owned objects only after checking names/schema.

        Firebird 2.5 requires committed metadata before dependent DDL/DML can be
        prepared. Each creation is independently restartable. Existing inactive
        or different triggers are reported, never CREATE OR ALTER'ed.
        """
        report = self.inspect_capabilities()
        created = []
        if report["conflicts"]:
            self.logger.warning("Captura Farmax requer reconciliação: %s", report["conflicts"])
        else:
            if not report["table_present"]:
                self._create("CREATE TABLE DELIVERYLOG (ID INTEGER NOT NULL PRIMARY KEY, "
                             "CD_VENDA DOUBLE PRECISION, ACTION VARCHAR(20), LOGDATE TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                created.append(self.LOG_TABLE_NAME)
                self._create("CREATE INDEX VELIDE_DELIVERYLOG_SALE ON DELIVERYLOG(CD_VENDA,ID)")
            if not report["sequence_present"]:
                # Seed a newly created generator from retained logs. Never reset
                # an existing generator: it may include uncommitted allocations.
                with self._engine.connect() as conn:
                    high = conn.execute(text("SELECT COALESCE(MAX(ID),0) FROM DELIVERYLOG")).scalar()
                with self._engine.begin() as conn:
                    conn.execute(text("CREATE SEQUENCE " + self.SEQUENCE_NAME))
                    conn.execute(text("SET GENERATOR " + self.SEQUENCE_NAME + " TO " + str(int(high))))
                created.append(self.SEQUENCE_NAME)
            for name, event in ((self.INCREMENT_TRIGGER_NAME, "BEFORE INSERT"),
                                (self.ADD_DELIVERY_TRIGGER_NAME, "AFTER INSERT OR UPDATE OR DELETE")):
                if name not in report["present_triggers"]:
                    relation, _, body = self.EXPECTED[name]
                    self._create("CREATE TRIGGER " + name + " FOR " + relation + " ACTIVE " + event + " POSITION 0 " + body)
                    created.append(name)
        report = self.inspect_capabilities()
        report["created_objects"] = created
        report["observe_only"] = not bool(created)  # startup DDL diagnostic, not a delivery-write gate
        self.logger.info("Captura de alterações Farmax: %s", report)
        return report

    def _create(self, statement):
        with self._engine.begin() as conn:
            conn.execute(text(statement))

    def inspect_capabilities(self):
        """Read-only diagnostics; unrelated native triggers do not block capture."""
        with self._engine.connect() as conn:
            relation = conn.execute(text("SELECT RDB$VIEW_BLR,RDB$EXTERNAL_FILE FROM RDB$RELATIONS WHERE RDB$RELATION_NAME='DELIVERYLOG'")).fetchone()
            table_present = relation is not None
            columns = {row[0].strip(): tuple(row[1:]) for row in conn.execute(text("""
                SELECT RF.RDB$FIELD_NAME,F.RDB$FIELD_TYPE,COALESCE(RF.RDB$NULL_FLAG,F.RDB$NULL_FLAG,0),
                       F.RDB$CHARACTER_LENGTH,COALESCE(F.RDB$FIELD_SCALE,0),F.RDB$COMPUTED_SOURCE FROM RDB$RELATION_FIELDS RF
                JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME=RF.RDB$FIELD_SOURCE
                WHERE RF.RDB$RELATION_NAME='DELIVERYLOG'"""))}
            log_available = {"ID", "CD_VENDA", "ACTION", "LOGDATE"}.issubset(columns)
            schema_matches = log_available and relation is not None and relation[0] is None and relation[1] is None
            schema_matches = schema_matches and len(columns) == 4 and columns["ID"][:2] == (8,1) and columns["CD_VENDA"][:2] == (27,0) and columns["ACTION"][:3] == (37,0,20) and columns["LOGDATE"][:2] == (35,0)
            schema_matches = schema_matches and all(value[3] == 0 and value[4] is None for value in columns.values())
            sequence_present = bool(conn.execute(text("SELECT 1 FROM RDB$GENERATORS WHERE RDB$GENERATOR_NAME=:name"), {"name": self.SEQUENCE_NAME}).fetchone())
            triggers = conn.execute(text("""SELECT RDB$TRIGGER_NAME,RDB$RELATION_NAME,RDB$TRIGGER_TYPE,RDB$TRIGGER_SEQUENCE,RDB$TRIGGER_SOURCE,
                COALESCE(RDB$TRIGGER_INACTIVE,0) FROM RDB$TRIGGERS
                WHERE RDB$TRIGGER_NAME IN ('TRG_DELIVERY_LOGID_INCREMENT','TRG_ADD_DELIVERY')""")).fetchall()
            actual = {row[0].strip(): row for row in triggers}
            conflicts = []
            if table_present and not schema_matches:
                conflicts.append("DELIVERYLOG possui esquema incompatível")
            capture_matches = schema_matches and sequence_present
            for name, (relation, kind, body) in self.EXPECTED.items():
                row = actual.get(name)
                matches = bool(row and row[1] and row[1].strip() == relation and row[2] == kind
                               and row[3] == 0 and sql_tokens(row[4]) == sql_tokens(body) and row[5] == 0)
                if row and not matches:
                    conflicts.append("Objeto existente preservado: " + name)
                capture_matches = capture_matches and matches
            indexes = [row[0].strip() for row in conn.execute(text("""SELECT S.RDB$FIELD_NAME FROM RDB$INDICES I
                JOIN RDB$INDEX_SEGMENTS S ON S.RDB$INDEX_NAME=I.RDB$INDEX_NAME WHERE I.RDB$RELATION_NAME='DELIVERYLOG'
                AND COALESCE(I.RDB$INDEX_INACTIVE,0)=0 AND S.RDB$FIELD_POSITION=0"""))]
        report = {"observe_only": True, "log_available": bool(schema_matches), "table_present": table_present,
                  "capture_matches_retained": bool(capture_matches), "leading_indexes": indexes,
                  "present_triggers": sorted(actual), "conflicts": conflicts,
                  "sequence_present": sequence_present, "identity_and_coverage_qualified": False,
                  "engine_version": getattr(self._engine.dialect, "server_version_info", None)}
        return report
