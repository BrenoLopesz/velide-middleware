from functools import wraps
import sqlite3
import logging
from typing import Optional, List, Tuple
from enum import Enum

def require_connection(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.conn:
            raise ConnectionError("Conexão fechada. Utilize o contexto 'with'.")
        return func(self, *args, **kwargs)
    return wrapper

class DeliveryStatus(Enum):
    """
    Defines the possible statuses for a delivery.
    The string value is what is stored in the database.
    """
    PENDING = "PENDENTE"
    IN_PROGRESS = "EM_ANDAMENTO"
    DELIVERED = "ENTREGUE"
    FAILED = "FALHA"
    CANCELLED = "CANCELADA"

class SQLiteManager:
    """
    Manages 'DeliverymenMapping' and 'DeliveryMapping' tables in a SQLite database.

    This class is designed to be used as a context manager to ensure
    that database connections are handled safely and automatically.

    Table 1 Schema:
    DeliverymenMapping (
        velide_id TEXT PRIMARY KEY NOT NULL,
        local_id  TEXT UNIQUE NOT NULL
    )
    
    Table 2 Schema:
    DeliveryMapping (
        external_delivery_id TEXT PRIMARY KEY NOT NULL,
        internal_delivery_id TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('PENDENTE', 'EM_ANDAMENTO', 'ENTREGUE', 'FALHA', 'CANCELADA'))
    )
    """

    def __init__(self, db_path: str):
        """
        Initializes the database manager.

        Args:
            db_path (str): The file path to the SQLite database.
        """
        if db_path is None:
            # This check is good, although the type hint `str` implies non-None.
            raise ValueError("É necessário informar o caminho para o banco de dados SQLite.")

        self.logger = logging.getLogger(__name__)
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> 'SQLiteManager':
        """
        Opens the database connection and creates the tables if they don't exist.
        
        This method is called when entering a 'with' statement.

        Returns:
            SQLiteManager: The current instance of the class.
        """
        try:
            self.conn = sqlite3.connect(self.db_path)
            # Enable WAL Mode
            # This allows concurrent readers and writers.
            self.conn.execute("PRAGMA journal_mode = WAL;")
            
            # Optimize Synchronization
            # 'NORMAL' is safe for WAL and much faster than default 'FULL'.
            self.conn.execute("PRAGMA synchronous = NORMAL;")
            
            # Foreign Keys
            self.conn.execute("PRAGMA foreign_keys = ON;")
            
            self._create_tables()
            return self
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao conectar ao banco de dados em {self.db_path}.")
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Closes the database connection, committing or rolling back changes.

        - If no exception occurred (exc_type is None), changes are committed.
        - If an exception occurred, changes are rolled back.
        
        This method is called when exiting a 'with' statement.
        """
        if self.conn:
            try:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.logger.warning(f"Desfazendo mudanças devido ao erro: {exc_val}")
                    self.conn.rollback()
            except sqlite3.Error as e:
                self.logger.exception(f"Erro durante saída.")
            finally:
                self.conn.close()
                self.conn = None

    @require_connection
    def _create_tables(self):
        """
        Internal method to create all required tables.
        
        Uses 'CREATE TABLE IF NOT EXISTS' to be idempotent (safe to run multiple times).
        """ 
        create_deliverymen_table_query = """
        CREATE TABLE IF NOT EXISTS DeliverymenMapping (
            velide_id TEXT PRIMARY KEY NOT NULL,
            local_id  TEXT UNIQUE NOT NULL
        );
        """
        
        # Get all valid enum status strings for the CHECK constraint
        status_values = ", ".join([f"'{s.value}'" for s in DeliveryStatus])
        
        create_delivery_table_query = f"""
        CREATE TABLE IF NOT EXISTS DeliveryMapping (
            external_delivery_id TEXT PRIMARY KEY NOT NULL,
            internal_delivery_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL CHECK(status IN ({status_values})),
            create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """

        # This trigger automatically updates 'updated_at' on any row update
        create_trigger_query = """
        CREATE TRIGGER IF NOT EXISTS update_delivery_mapping_timestamp
        AFTER UPDATE ON DeliveryMapping
        FOR EACH ROW
        BEGIN
            UPDATE DeliveryMapping
            SET updated_at = CURRENT_TIMESTAMP
            WHERE external_delivery_id = OLD.external_delivery_id;
        END;
        """
        
        try:
            self.conn.execute(create_deliverymen_table_query)
            self.conn.execute(create_delivery_table_query)
            self.conn.execute(create_trigger_query)
        except sqlite3.Error as e:
            self.logger.exception("Falha ao criar tabelas ou trigger.")
            raise
    # -----------------------------------------------------------------
    # DeliverymenMapping Methods
    # -----------------------------------------------------------------

    @require_connection
    def add_mapping(self, velide_id: str, local_id: str) -> bool:
        """
        Adds a new mapping between a velide_id and a local_id.

        Args:
            velide_id (str): The Velide ID.
            local_id (str): The Local ID.

        Returns:
            bool: True if the mapping was added successfully, False if a
                  constraint (PRIMARY KEY or UNIQUE) was violated.
        """
        insert_query = "INSERT INTO DeliverymenMapping (velide_id, local_id) VALUES (?, ?)"
        try:
            self.conn.execute(insert_query, (velide_id, local_id))
            self.logger.debug(f"Adicionado mapeamento: {velide_id} -> {local_id}")
            return True
        except sqlite3.IntegrityError as e:
            # This catches violations of PRIMARY KEY (velide_id) or
            # UNIQUE (local_id) constraints.
            self.logger.warning(f"Falha ao mapear ({velide_id}, {local_id}). Motivo: {e}")
            return False
        except sqlite3.Error as e:
            self.logger.exception("Ocorreu um erro inesperado ao adicionar um mapeamento.")
            return False
        
    @require_connection
    def add_many_mappings(self, mappings: List[Tuple[str, str]]) -> int:
        """
        Adds multiple mappings in a single transaction, ignoring duplicates.

        Uses "INSERT OR IGNORE" to skip rows that would violate
        PRIMARY KEY (velide_id) or UNIQUE (local_id) constraints.

        Args:
            mappings: A list of (velide_id, local_id) tuples.

        Returns:
            int: The number of rows actually inserted.
        """
        if not mappings:
            self.logger.warning("Nenhuma mapeamento fornecido para 'add_many_mappings'.")
            return 0

        # Use INSERT OR IGNORE to skip duplicates without raising an error
        insert_query = "INSERT OR IGNORE INTO DeliverymenMapping (velide_id, local_id) VALUES (?, ?)"
        
        try:
            cursor = self.conn.executemany(insert_query, mappings)
            inserted_count = cursor.rowcount
            self.logger.debug(f"Processados {len(mappings)} mapeamentos. {inserted_count} novos inseridos.")
            return inserted_count
        except sqlite3.Error as e:
            self.logger.exception("Ocorreu um erro inesperado durante o 'add_many_mappings'.")
            # The __exit__ method will handle the rollback.
            raise # Re-raise to trigger rollback in __exit__

    @require_connection
    def get_local_id(self, velide_id: str) -> Optional[str]:
        """
        Retrieves the local_id associated with a given velide_id.

        Args:
            velide_id (str): The Velide ID to search for.

        Returns:
            Optional[str]: The corresponding local_id if found, else None.
        """ 
        query = "SELECT local_id FROM DeliverymenMapping WHERE velide_id = ?"
        try:
            cursor = self.conn.execute(query, (velide_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao buscar `local_id` para {velide_id}.")
            return None

    @require_connection
    def get_velide_id(self, local_id: str) -> Optional[str]:
        """
        Retrieves the velide_id associated with a given local_id.

        Args:
            local_id (str): The Local ID to search for.

        Returns:
            Optional[str]: The corresponding velide_id if found, else None.
        """
        query = "SELECT velide_id FROM DeliverymenMapping WHERE local_id = ?"
        try:
            cursor = self.conn.execute(query, (local_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            self.logger.error(f"Error ao buscar `velide_id` para {local_id}: {e}")
            return None

    @require_connection
    def delete_mapping_by_velide_id(self, velide_id: str) -> bool:
        """
        Deletes a mapping from the table based on the velide_id.

        Args:
            velide_id (str): The Velide ID of the mapping to delete.

        Returns:
            bool: True if a row was deleted, False otherwise.
        """
        query = "DELETE FROM DeliverymenMapping WHERE velide_id = ?"
        try:
            cursor = self.conn.execute(query, (velide_id,))
            if cursor.rowcount > 0:
                self.logger.info(f"Mapeamento deletado para o `velide_id`: {velide_id}")
                return True
            else:
                self.logger.warning(f"Nenhum mapeamento encontrado para deletar o velide_id: {velide_id}")
                return False
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao deletar mapeamento de {velide_id}.")
            return False

    @require_connection
    def get_all_mappings(self) -> List[Tuple[str, str]]:
        """
        Retrieves all mappings from the table.

        Returns:
            List[Tuple[str, str]]: A list of (velide_id, local_id) tuples.
        """
        query = "SELECT velide_id, local_id FROM DeliverymenMapping"
        try:
            cursor = self.conn.execute(query)
            return cursor.fetchall()
        except sqlite3.Error as e:
            self.logger.error(f"Erro ao buscar todos os mapeamentos: {e}")
            return []

    # -----------------------------------------------------------------
    # DeliveryMapping Methods
    # -----------------------------------------------------------------

    @require_connection
    def add_delivery_mapping(self, external_id: str, internal_id: str, status: DeliveryStatus) -> bool:
        """
        Adds a new delivery mapping.

        Args:
            external_id (str): The external delivery ID.
            internal_id (str): The internal delivery ID.
            status (DeliveryStatus): The initial status of the delivery.

        Returns:
            bool: True if added successfully, False on a constraint violation.
        """
        query = "INSERT INTO DeliveryMapping (external_delivery_id, internal_delivery_id, status) VALUES (?, ?, ?)"
        try:
            self.conn.execute(query, (external_id, internal_id, status.value))
            self.logger.debug(f"Adicionado mapeamento de entrega: {external_id} -> {internal_id} (Status: {status.name})")
            return True
        except sqlite3.IntegrityError as e:
            self.logger.warning(f"Falha ao mapear entrega ({external_id}, {internal_id}). Motivo: {e}")
            return False
        except sqlite3.Error as e:
            self.logger.exception("Ocorreu um erro inesperado ao adicionar um mapeamento de entrega.")
            return False

    @require_connection
    def add_many_delivery_mappings(self, mappings: List[Tuple[str, str, DeliveryStatus]]) -> int:
        """
        Adds multiple delivery mappings, ignoring duplicates.

        Uses "INSERT OR IGNORE" to skip rows that would violate
        PRIMARY KEY (external_delivery_id) or UNIQUE (internal_delivery_id) constraints.

        Args:
            mappings: A list of (external_id, internal_id, status) tuples.

        Returns:
            int: The number of rows actually inserted.
        """
        if not mappings:
            self.logger.warning("Nenhuma mapeamento de entrega fornecido para 'add_many_delivery_mappings'.")
            return 0
        
        # Convert Enum objects to their string values for the database
        data_to_insert = [(ext, int_id, stat.value) for ext, int_id, stat in mappings]
        
        query = "INSERT OR IGNORE INTO DeliveryMapping (external_delivery_id, internal_delivery_id, status) VALUES (?, ?, ?)"
        
        try:
            cursor = self.conn.executemany(query, data_to_insert)
            inserted_count = cursor.rowcount
            self.logger.debug(f"Processados {len(mappings)} mapeamentos de entrega. {inserted_count} novos inseridos.")
            return inserted_count
        except sqlite3.Error as e:
            self.logger.exception("Ocorreu um erro inesperado durante o 'add_many_delivery_mappings'.")
            raise # Re-raise to trigger rollback in __exit__

    @require_connection
    def update_delivery_status(self, external_id: str, new_status: DeliveryStatus) -> bool:
        """
        Updates the status of an existing delivery mapping.

        Args:
            external_id (str): The external ID of the delivery to update.
            new_status (DeliveryStatus): The new status to set.

        Returns:
            bool: True if a row was updated, False if no matching row was found.
        """
        query = "UPDATE DeliveryMapping SET status = ? WHERE external_delivery_id = ?"
        try:
            cursor = self.conn.execute(query, (new_status.value, external_id))
            if cursor.rowcount > 0:
                self.logger.info(f"Status da entrega {external_id} atualizado para {new_status.name}")
                return True
            else:
                self.logger.warning(f"Nenhuma entrega encontrada para atualizar status (ID: {external_id})")
                return False
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao atualizar status da entrega {external_id}.")
            return False

    @require_connection
    def get_delivery_by_external_id(self, external_id: str) -> Optional[Tuple[str, DeliveryStatus]]:
        """
        Retrieves a delivery's internal ID and status using its external ID.

        Args:
            external_id (str): The external delivery ID to search for.

        Returns:
            Optional[Tuple[str, DeliveryStatus]]: A tuple of
            (internal_delivery_id, status) if found, else None.
        """ 
        query = "SELECT internal_delivery_id, status FROM DeliveryMapping WHERE external_delivery_id = ?"
        try:
            cursor = self.conn.execute(query, (external_id,))
            result = cursor.fetchone()
            if result:
                # Convert the status string back to a DeliveryStatus enum object
                return (result[0], DeliveryStatus(result[1]))
            return None
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao buscar entrega com external_id {external_id}.")
            return None
        except ValueError as e: # Catch errors if status in DB is not in Enum
            self.logger.error(f"Status inválido no DB para entrega {external_id}: {e}")
            return None

    @require_connection
    def get_delivery_by_internal_id(self, internal_id: str) -> Optional[Tuple[str, DeliveryStatus]]:
        """
        Retrieves a delivery's external ID and status using its internal ID.

        Args:
            internal_id (str): The internal delivery ID to search for.

        Returns:
            Optional[Tuple[str, DeliveryStatus]]: A tuple of
            (external_delivery_id, status) if found, else None.
        """ 
        query = "SELECT external_delivery_id, status FROM DeliveryMapping WHERE internal_delivery_id = ?"
        try:
            cursor = self.conn.execute(query, (internal_id,))
            result = cursor.fetchone()
            if result:
                # Convert the status string back to a DeliveryStatus enum object
                return (result[0], DeliveryStatus(result[1]))
            return None
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao buscar entrega com internal_id {internal_id}.")
            return None
        except ValueError as e: # Catch errors if status in DB is not in Enum
            self.logger.error(f"Status inválido no DB para entrega {internal_id}: {e}")
            return None

    @require_connection
    def get_all_deliveries(self) -> List[Tuple[str, str, DeliveryStatus]]:
        """
        Retrieves all delivery mappings from the table.

        Returns:
            List[Tuple[str, str, DeliveryStatus]]: A list of
            (external_delivery_id, internal_delivery_id, status) tuples.
        """
        query = "SELECT external_delivery_id, internal_delivery_id, status FROM DeliveryMapping"
        try:
            cursor = self.conn.execute(query)
            rows = cursor.fetchall()
            # Convert all status strings to Enum objects
            return [(row[0], row[1], DeliveryStatus(row[2])) for row in rows]
        except sqlite3.Error as e:
            self.logger.error(f"Erro ao buscar todos os mapeamentos de entrega: {e}")
            return []
        except ValueError as e:
            self.logger.error(f"Erro ao converter status do DB para Enum: {e}")
            return []