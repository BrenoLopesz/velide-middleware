import logging

from api.sqlite_manager import SQLiteManager

class TrackingPersistenceService:

    def __init__(self, sqlite_manager: SQLiteManager):
        self._sqlite = sqlite_manager

        with sqlite_manager as sqlite:
            sqlite._create_tables()

    def load_tracked_state(self):
        """Loads all persisted IDs and statuses for a specific strategy."""
        tracked_ids = set()
        tracked_statuses = {}

        with self._sqlite as sqlite:
            all_deliveries = sqlite.get_all_deliveries()
            for row in all_deliveries:
                external_id = row[0]
                internal_id = row[1]
                status = row[2]
                tracked_ids.add(internal_id)
                tracked_statuses[internal_id] = status