"""Read-only capability inspection; startup never installs or repairs ERP DDL."""
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
        "TG_AT_ENVIADO_ENTREGAS": ("ENTREGAS", 17, "AS BEGIN NEW.ENVIADO = ' '; END"),
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
        """Compatibility name retained for callers; performs SELECTs only."""
        with self._engine.connect() as conn:
            columns = {row[0].strip(): tuple(row[1:]) for row in conn.execute(text("""
                SELECT RF.RDB$FIELD_NAME,F.RDB$FIELD_TYPE,COALESCE(RF.RDB$NULL_FLAG,F.RDB$NULL_FLAG,0),
                       F.RDB$CHARACTER_LENGTH,COALESCE(F.RDB$FIELD_SCALE,0),F.RDB$COMPUTED_SOURCE FROM RDB$RELATION_FIELDS RF
                JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME=RF.RDB$FIELD_SOURCE
                WHERE RF.RDB$RELATION_NAME='DELIVERYLOG'"""))}
            log_available = {"ID", "CD_VENDA", "ACTION", "LOGDATE"}.issubset(columns)
            schema_matches = log_available and len(columns) == 4 and columns["ID"][:2] == (8,1) and columns["CD_VENDA"][:2] == (27,0) and columns["ACTION"][:3] == (37,0,20) and columns["LOGDATE"][:2] == (35,0)
            schema_matches = schema_matches and all(value[3] == 0 and value[4] is None for value in columns.values())
            sequence_present = bool(conn.execute(text("SELECT 1 FROM RDB$GENERATORS WHERE RDB$GENERATOR_NAME=:name"), {"name": self.SEQUENCE_NAME}).fetchone())
            triggers = conn.execute(text("""SELECT RDB$TRIGGER_NAME,RDB$RELATION_NAME,RDB$TRIGGER_TYPE,RDB$TRIGGER_SEQUENCE,RDB$TRIGGER_SOURCE
                FROM RDB$TRIGGERS WHERE RDB$RELATION_NAME IN ('ENTREGAS','DELIVERYLOG')
                AND COALESCE(RDB$SYSTEM_FLAG,0)=0 AND COALESCE(RDB$TRIGGER_INACTIVE,0)=0""")).fetchall()
            actual = {row[0].strip(): row for row in triggers}
            capture_matches = schema_matches and sequence_present and set(actual) == set(self.EXPECTED)
            for name, (relation, kind, body) in self.EXPECTED.items():
                row = actual.get(name)
                capture_matches = capture_matches and bool(row and row[1].strip() == relation and row[2] == kind
                    and row[3] == 0 and sql_tokens(row[4]) == sql_tokens(body))
            indexes = [row[0].strip() for row in conn.execute(text("""SELECT S.RDB$FIELD_NAME FROM RDB$INDICES I
                JOIN RDB$INDEX_SEGMENTS S ON S.RDB$INDEX_NAME=I.RDB$INDEX_NAME WHERE I.RDB$RELATION_NAME='DELIVERYLOG'
                AND COALESCE(I.RDB$INDEX_INACTIVE,0)=0 AND S.RDB$FIELD_POSITION=0"""))]
        report = {"observe_only": True, "log_available": log_available,
                  "capture_matches_retained": bool(capture_matches), "leading_indexes": indexes,
                  "sequence_present": sequence_present, "identity_and_coverage_qualified": False,
                  "engine_version": getattr(self._engine.dialect, "server_version_info", None)}
        self.logger.warning("Farmax observer capabilities: %s. This is not cashier/workflow qualification.", report)
        return report
