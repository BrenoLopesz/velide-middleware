import logging
from textwrap import dedent

from sqlalchemy import text, Connection, Engine
from sqlalchemy.exc import SQLAlchemyError


class FarmaxSetup:
    SEQUENCE_NAME = "DELIVERYLOG_ID_AUTOINCREMENT"
    LOG_TABLE_NAME = "DELIVERYLOG"
    INCREMENT_TRIGGER_NAME = "TRG_DELIVERY_LOGID_INCREMENT"
    ADD_DELIVERY_TRIGGER_NAME = "TRG_ADD_DELIVERY"

    def __init__(self, engine: Engine):
        self.logger = logging.getLogger(__name__)
        self._engine = engine

    def _check_if_object_exists(
        self, conn: Connection, object_name: str, rdb_table: str
    ):
        """Helper to check Firebird system tables."""
        field_name = (
            "RDB$GENERATOR_NAME"
            if rdb_table == "RDB$GENERATORS"
            else "RDB$RELATION_NAME"
        )

        query = text(f"SELECT 1 FROM {rdb_table} WHERE {field_name} = :name")
        result = conn.execute(query, {"name": object_name.upper()})
        return result.fetchone() is not None

    def check_if_table_exists(self, conn: Connection, table_name: str):
        return self._check_if_object_exists(conn, table_name, "RDB$RELATIONS")

    def check_if_sequence_exists(self, conn: Connection, seq_name: str):
        return self._check_if_object_exists(conn, seq_name, "RDB$GENERATORS")

    def _setup_sequence(self, conn: Connection):
        """Creates the delivery log sequence if it doesn't exist."""
        if not self.check_if_sequence_exists(conn, self.SEQUENCE_NAME):
            if self.check_if_table_exists(conn, self.LOG_TABLE_NAME):
                if conn.execute(text(f"SELECT FIRST 1 ID FROM {self.LOG_TABLE_NAME}")).fetchone():
                    raise ValueError("Existing delivery log has records but its sequence is missing; reconcile it before startup")
            self.logger.debug(f"Criando sequência: {self.SEQUENCE_NAME}")
            query = text(f"CREATE SEQUENCE {self.SEQUENCE_NAME}")
            conn.execute(query)
        else:
            self.logger.debug(f"Sequência já existe: {self.SEQUENCE_NAME}")

    def _setup_log_table(self, conn: Connection):
        """Creates the delivery log table if it doesn't exist."""
        if not self.check_if_table_exists(conn, self.LOG_TABLE_NAME):
            self.logger.debug(f"Criando tabela: {self.LOG_TABLE_NAME}")

            # Using dedent makes the multi-line SQL much cleaner
            query_str = dedent(f"""
                CREATE TABLE {self.LOG_TABLE_NAME} (
                    Id INTEGER PRIMARY KEY,
                    CD_VENDA DOUBLE PRECISION,
                    Action VARCHAR(20),
                    LogDate TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute(text(query_str))
        else:
            self.logger.debug(f"Tabela já existe: {self.LOG_TABLE_NAME}")

    def _setup_increment_trigger(self, conn: Connection):
        """Creates or alters the trigger to auto-increment the log table ID."""
        if self._installed_trigger(conn, self.INCREMENT_TRIGGER_NAME, self.LOG_TABLE_NAME):
            return
        self.logger.debug(f"Criando trigger: {self.INCREMENT_TRIGGER_NAME}")
        query_str = dedent(f"""
            CREATE OR ALTER TRIGGER {self.INCREMENT_TRIGGER_NAME}
            FOR {self.LOG_TABLE_NAME}
            ACTIVE BEFORE INSERT POSITION 0
            AS
            BEGIN
                NEW.Id = next value for {self.SEQUENCE_NAME};
            END""")
        conn.execute(text(query_str))

    def _setup_delivery_log_trigger(self, conn: Connection):
        """Creates or alters the trigger to log changes to the ENTREGAS table."""
        if self._installed_trigger(conn, self.ADD_DELIVERY_TRIGGER_NAME, "ENTREGAS"):
            return
        self.logger.debug(
            f"Criando/Alterando trigger: {self.ADD_DELIVERY_TRIGGER_NAME}"
        )
        # FIX: Added CURRENT_TIMESTAMP back to the INSERT statements
        query_str = dedent(f"""
            CREATE OR ALTER TRIGGER {self.ADD_DELIVERY_TRIGGER_NAME}
            FOR ENTREGAS
            ACTIVE AFTER INSERT OR UPDATE OR DELETE
            AS
            BEGIN
                IF (INSERTING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action, LogDate)
                    VALUES (NEW.CD_VENDA, 'INSERT', CURRENT_TIMESTAMP);
                ELSE IF (UPDATING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action, LogDate)
                    VALUES (NEW.CD_VENDA, 'UPDATE', CURRENT_TIMESTAMP);
                ELSE IF (DELETING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action, LogDate)
                    VALUES (OLD.CD_VENDA, 'DELETE', CURRENT_TIMESTAMP);
            END""")
        conn.execute(text(query_str))

    def _installed_trigger(self, conn, name, relation):
        row = conn.execute(text("SELECT RDB$RELATION_NAME,RDB$TRIGGER_INACTIVE FROM RDB$TRIGGERS WHERE RDB$TRIGGER_NAME=:name"), {"name": name}).fetchone()
        if row and (row[0].strip() != relation or row[1]):
            raise ValueError("Installed integration trigger is inactive or belongs to another relation: " + name)
        return row is not None

    def _validate_existing_log(self, conn):
        fields = {row[0].strip() for row in conn.execute(text("SELECT RDB$FIELD_NAME FROM RDB$RELATION_FIELDS WHERE RDB$RELATION_NAME=:name"), {"name": self.LOG_TABLE_NAME})}
        if not {"ID", "CD_VENDA", "ACTION", "LOGDATE"}.issubset(fields):
            raise ValueError("Existing DELIVERYLOG schema is incompatible")
        indexed = {row[0].strip() for row in conn.execute(text("""SELECT S.RDB$FIELD_NAME
            FROM RDB$INDICES I JOIN RDB$INDEX_SEGMENTS S ON S.RDB$INDEX_NAME=I.RDB$INDEX_NAME
            WHERE I.RDB$RELATION_NAME=:name AND COALESCE(I.RDB$INDEX_INACTIVE,0)=0
              AND S.RDB$FIELD_POSITION=0"""), {"name": self.LOG_TABLE_NAME})}
        if not {"ID", "CD_VENDA"}.issubset(indexed):
            self.logger.warning("DELIVERYLOG lacks leading ID/CD_VENDA indexes; measure query plans before changing polling intervals.")

    def initial_setup(self):
        """
        Add necessary tables, sequences, and triggers to track deliveries.
        This operation is idempotent and safe to run multiple times.
        """

        # The 'with' block just gets a connection.
        # Transaction logic must be handled inside.
        try:
            with self._engine.begin() as conn:
                # Call each step inside one transaction
                self._setup_sequence(conn)
                self._setup_log_table(conn)
                self._setup_increment_trigger(conn)
                self._setup_delivery_log_trigger(conn)
                self._validate_existing_log(conn)

                self.logger.info("Setup inicial do Farmax concluído.")

        # Use a more specific exception
        except SQLAlchemyError:
            self.logger.exception(
                "Falha ao realizar setup inicial do database do Farmax."
            )
            raise
        except Exception:
            self.logger.exception("Um erro inesperado ocorreu durante o setup.")
            raise
