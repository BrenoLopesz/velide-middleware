# services/velide_gateway.py
from threading import RLock
from typing import Optional

from api.velide import Velide
from config import ApiConfig, ReconciliationConfig, TargetSystem


class VelideGateway:
    """
    Thread-safe gateway for generating authenticated Velide API clients.
    
    This class acts as a Factory. Instead of holding a single shared 'Velide'
    instance (Singleton), it stores the necessary credentials (access token)
    and configuration to manufacture a fresh, isolated 'Velide' client
    for every request.
    
    This design is required for concurrent environments (like PyQt + asyncio)
    where sharing an HTTP client across threads/loops causes RuntimeErrors.
    """

    def __init__(
        self,
        api_config: ApiConfig,
        target_system: TargetSystem,
        reconciliation_config: Optional[ReconciliationConfig] = None,
    ):
        """
        Initialize the gateway with static configuration.

        Args:
            api_config: Global API settings (URL, timeouts, SSL).
            target_system: The integration target identifier.
            reconciliation_config: Optional configuration for reconciliation on retry.
        """
        self._access_token: Optional[str] = None
        self._lock = RLock()
        self.config = api_config
        self.target = target_system
        self._reconciliation_config = reconciliation_config

    def update_token(self, access_token: str) -> None:
        """
        Updates the stored authentication token.
        
        This should be called by the AuthService whenever a login occurs
        or a token is refreshed.

        Args:
            access_token: The new Bearer token string.
        """
        with self._lock:
            self._access_token = access_token

    def get_client(self) -> Optional[Velide]:
        """
        Constructs and returns a new Velide API client instance.

        Returns:
            Velide: A fresh instance initialized with the current token.
            None: If the gateway has not yet received a valid access token.
        """
        with self._lock:
            if not self._access_token:
                return None
            
            # FACTORY PATTERN:
            # We return a new instance every time. This ensures that the
            # httpx.AsyncClient created inside 'Velide' belongs strictly
            # to the thread/loop that requested it.
            return Velide(
                self._access_token,
                self.config,
                self.target,
                self._reconciliation_config,
            )

    def is_ready(self) -> bool:
        """
        Checks if the gateway has valid credentials to issue clients.

        Returns:
            bool: True if an access token is present, False otherwise.
        """
        with self._lock:
            return self._access_token is not None