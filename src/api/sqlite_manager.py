import sqlite3
from sqlite3 import Error

class SQLiteManager:
    """
    A context manager class for simplified SQLite database operations.

    Args:
        db_file (str): The path to the SQLite database file.
    """
    def __init__(self, db_file):
        self.db_file = db_file
        self.conn = None

    def __enter__(self):
        """
        Connect to the SQLite database and return the connection object.
        """
        try:
            self.conn = sqlite3.connect(self.db_file)
            self.conn.row_factory = sqlite3.Row  # Access columns by name
            print(f"Successfully connected to {self.db_file}")
            return self
        except Error as e:
            print(f"Error connecting to database: {e}")
            raise  # Re-raise the exception to stop the 'with' block

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Close the database connection.
        """
        if self.conn:
            self.conn.close()
            print(f"Database connection to {self.db_file} closed.")
            
    def execute_query(self, query, params=()):
        """
        Execute a single SQL query (e.g., CREATE TABLE, INSERT, UPDATE, DELETE).

        Args:
            query (str): The SQL query to execute.
            params (tuple, optional): Parameters to bind to the query.
        """
        if not self.conn:
            raise ConnectionError("Database is not connected.")
            
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            self.conn.commit()
            print("Query executed successfully.")
            return cursor.lastrowid  # Useful for getting the ID of an INSERT
        except Error as e:
            print(f"Error executing query: {e}")
            self.conn.rollback() # Rollback changes if an error occurs
            return None

    def fetch_all(self, query, params=()):
        """
        Execute a SELECT query and fetch all results.

        Args:
            query (str): The SELECT query to execute.
            params (tuple, optional): Parameters to bind to the query.

        Returns:
            list: A list of rows (as sqlite3.Row objects).
        """
        if not self.conn:
            raise ConnectionError("Database is not connected.")

        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return rows
        except Error as e:
            print(f"Error fetching data: {e}")
            return []

    def fetch_one(self, query, params=()):
        """
        Execute a SELECT query and fetch the first result.

        Args:
            query (str): The SELECT query to execute.
            params (tuple, optional): Parameters to bind to the query.

        Returns:
            sqlite3.Row or None: A single row object or None if no result.
        """
        if not self.conn:
            raise ConnectionError("Database is not connected.")
            
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            row = cursor.fetchone()
            return row
        except Error as e:
            print(f"Error fetching data: {e}")
            return None

# --- Example Usage ---

if __name__ == "__main__":
    
    db_path = "example.db"

    # Use the context manager to handle the connection
    try:
        with SQLiteManager(db_path) as db:
            
            # 1. Create a table
            print("\n--- Creating table ---")
            create_users_table_query = """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE
            );
            """
            db.execute_query(create_users_table_query)

            # 2. Insert data
            print("\n--- Inserting data ---")
            # Using placeholders (?) to prevent SQL injection
            db.execute_query("INSERT INTO users (name, email) VALUES (?, ?)", 
                             ("Alice", "alice@example.com"))
            db.execute_query("INSERT INTO users (name, email) VALUES (?, ?)", 
                             ("Bob", "bob@example.com"))

            # 3. Query all data
            print("\n--- Fetching all users ---")
            all_users = db.fetch_all("SELECT * FROM users")
            if all_users:
                for user in all_users:
                    # Access data by column name (due to row_factory)
                    print(f"ID: {user['id']}, Name: {user['name']}, Email: {user['email']}")

            # 4. Query one item
            print("\n--- Fetching one user (Alice) ---")
            user_alice = db.fetch_one("SELECT * FROM users WHERE name = ?", ("Alice",))
            if user_alice:
                print(f"Found: {user_alice['name']} ({user_alice['email']})")

            # 5. Example of handling a failed query (UNIQUE constraint)
            print("\n--- Attempting duplicate insert ---")
            db.execute_query("INSERT INTO users (name, email) VALUES (?, ?)", 
                             ("Charlie", "alice@example.com")) # This will fail

    except Error as e:
        print(f"An operation failed: {e}")
    except ConnectionError as e:
        print(f"Connection failed: {e}")