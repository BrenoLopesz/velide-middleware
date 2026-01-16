# services/velide_gateway.py
from threading import RLock
from typing import Optional
from api.velide import Velide
from config import ApiConfig, TargetSystem

class VelideGateway:
    def __init__(
        self, 
        api_config: ApiConfig, 
        target_system: TargetSystem
    ):
        self._client: Optional[Velide] = None
        self._lock = RLock()
        self.config = api_config
        self.target = target_system

    def update_token(self, access_token: str) -> None:
        """Called by AuthService when login or refresh happens."""
        with self._lock:
            self._client = Velide(access_token, self.config, self.target)

    def get_client(self) -> Optional[Velide]:
        """Called by Workers to get the API client."""
        with self._lock:
            return self._client

    def is_ready(self):
        with self._lock: # Lock acquired here (2nd time) - RLock allows this!
            return self._client is not None