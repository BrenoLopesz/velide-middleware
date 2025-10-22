import sqlite3
import logging
from typing import Optional, List, Tuple

class SQLiteManager:
    """
    Manages the 'DeliverymenMapping' table in a SQLite database.

    This class is designed to be used as a context manager to ensure
    that database connections are handled safely and automatically.

    The table schema is:
    DeliverymenMapping (
        velide_id TEXT PRIMARY KEY NOT NULL,
        local_id  TEXT UNIQUE NOT NULL
    )
    
    - 'velide_id' is the primary key, which must be unique and not null.
    - 'local_id' has a UNIQUE constraint, so it must also be unique and not null.
    """

    def __init__(self, db_path: str):
        """
        Initializes the database manager.

        Args:
            db_path (str): The file path to the SQLite database.
                           Defaults to "deliverymen.db" in the current directory.
        """
        if db_path is None:
            raise ValueError("É necessário informar o caminho para o banco de dados SQLite.")

        self.logger = logging.getLogger(__name__)
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> 'SQLiteManager':
        """
        Opens the database connection and creates the table if it doesn't exist.
        
        This method is called when entering a 'with' statement.

        Returns:
            DeliverymenMappingDB: The current instance of the class.
        """
        try:
            self.conn = sqlite3.connect(self.db_path)
            # Enable foreign key support just in case (good practice)
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self._create_table()
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

    def _create_table(self):
        """
        Internal method to create the 'DeliverymenMapping' table.
        
        Uses 'CREATE TABLE IF NOT EXISTS' to be idempotent (safe to run multiple times).
        """
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta.")
            
        create_table_query = """
        CREATE TABLE IF NOT EXISTS DeliverymenMapping (
            velide_id TEXT PRIMARY KEY NOT NULL,
            local_id  TEXT UNIQUE NOT NULL
        );
        """
        try:
            self.conn.execute(create_table_query)
        except sqlite3.Error as e:
            self.logger.exception("Falha ao criar tabela.")
            raise

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
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta. Utilize o 'with'.")
        
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

    def get_local_id(self, velide_id: str) -> Optional[str]:
        """
        Retrieves the local_id associated with a given velide_id.

        Args:
            velide_id (str): The Velide ID to search for.

        Returns:
            Optional[str]: The corresponding local_id if found, else None.
        """
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta. Utilize o 'with'.")
            
        query = "SELECT local_id FROM DeliverymenMapping WHERE velide_id = ?"
        try:
            cursor = self.conn.execute(query, (velide_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            self.logger.exception(f"Erro ao buscar `local_id` para {velide_id}.")
            return None

    def get_velide_id(self, local_id: str) -> Optional[str]:
        """
        Retrieves the velide_id associated with a given local_id.

        Args:
            local_id (str): The Local ID to search for.

        Returns:
            Optional[str]: The corresponding velide_id if found, else None.
        """
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta. Utilize o 'with'.")
                    
        query = "SELECT velide_id FROM DeliverymenMapping WHERE local_id = ?"
        try:
            cursor = self.conn.execute(query, (local_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        except sqlite3.Error as e:
            self.logger.error(f"Error ao buscar `velide_id` para {local_id}: {e}")
            return None

    def delete_mapping_by_velide_id(self, velide_id: str) -> bool:
        """
        Deletes a mapping from the table based on the velide_id.

        Args:
            velide_id (str): The Velide ID of the mapping to delete.

        Returns:
            bool: True if a row was deleted, False otherwise.
        """
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta. Utilize o 'with'.")
            
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

    def get_all_mappings(self) -> List[Tuple[str, str]]:
        """
        Retrieves all mappings from the table.

        Returns:
            List[Tuple[str, str]]: A list of (velide_id, local_id) tuples.
        """
        if not self.conn:
            raise ConnectionError("Conexão com banco de dados não está aberta. Utilize o 'with'.")
        
        query = "SELECT velide_id, local_id FROM DeliverymenMapping"
        try:
            cursor = self.conn.execute(query)
            return cursor.fetchall()
        except sqlite3.Error as e:
            self.logger.error(f"Erro ao buscar todos os mapeamentos: {e}")
            return []