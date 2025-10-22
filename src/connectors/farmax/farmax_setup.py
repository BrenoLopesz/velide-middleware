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

    def _check_if_object_exists(self, conn: Connection, object_name: str, rdb_table: str):
        """Helper to check Firebird system tables."""
        field_name = "RDB$GENERATOR_NAME" if rdb_table == "RDB$GENERATORS" else "RDB$RELATION_NAME"
        
        query = text(
            f"SELECT 1 FROM {rdb_table} WHERE {field_name} = :name"
        )
        result = conn.execute(query, {"name": object_name.upper()})
        return result.fetchone() is not None

    def check_if_table_exists(self, conn: Connection, table_name: str):
        return self._check_if_object_exists(conn, table_name, "RDB$RELATIONS")

    def check_if_sequence_exists(self, conn: Connection, seq_name: str):
        return self._check_if_object_exists(conn, seq_name, "RDB$GENERATORS")

    def _setup_sequence(self, conn: Connection):
        """Creates the delivery log sequence if it doesn't exist."""
        if not self.check_if_sequence_exists(conn, self.SEQUENCE_NAME):
            self.logger.info(f"Creating sequence: {self.SEQUENCE_NAME}")
            query = text(f"CREATE SEQUENCE {self.SEQUENCE_NAME}")
            conn.execute(query)
        else:
            self.logger.debug(f"Sequence already exists: {self.SEQUENCE_NAME}")

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
            self.logger.debug(f"Table already exists: {self.LOG_TABLE_NAME}")

    def _setup_increment_trigger(self, conn: Connection):
        """Creates or alters the trigger to auto-increment the log table ID."""
        self.logger.debug(f"Criando/Alterando trigger: {self.INCREMENT_TRIGGER_NAME}")
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
        self.logger.debug(f"Criando/Alterando trigger: {self.ADD_DELIVERY_TRIGGER_NAME}")
        query_str = dedent(f"""
            CREATE OR ALTER TRIGGER {self.ADD_DELIVERY_TRIGGER_NAME}
            FOR ENTREGAS
            ACTIVE AFTER INSERT OR UPDATE OR DELETE
            AS
            BEGIN
                IF (INSERTING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action)
                    VALUES (NEW.CD_VENDA, 'INSERT');
                ELSE IF (UPDATING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action)
                    VALUES (NEW.CD_VENDA, 'UPDATE');
                ELSE IF (DELETING) THEN
                    INSERT INTO {self.LOG_TABLE_NAME} (CD_VENDA, Action)
                    VALUES (OLD.CD_VENDA, 'DELETE');
            END""")
        conn.execute(text(query_str))

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
                
                self.logger.info("Setup inicial do Farmax concluído.")

        # Use a more specific exception
        except SQLAlchemyError:
            self.logger.exception(f"Falha ao realizar setup inicial do database do Farmax.")
            raise
        except Exception:
            self.logger.exception(f"Um erro inesperado ocorreu durante o setup.")
            raise