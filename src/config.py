from enum import Enum
import os
from pathlib import Path
from typing import Optional, Type, TypeVar
from pydantic import BaseModel, Field, field_validator
import yaml

from utils.bundle_dir import BUNDLE_DIR

T = TypeVar("T", bound="Settings")


class TargetSystem(str, Enum):
    CDS = "CDS"
    FARMAX = "Farmax"

class ReconciliationConfig(BaseModel):
    """
    Configuration settings for the ReconciliationService.
    Controls synchronization intervals and cache coherency rules.
    """
    enabled: bool = Field(
        default=True,
        description=("Master switch to enable/disable the "
                    "automatic background reconciliation loop.")
    )

    sync_interval_ms: int = Field(
        default=60_000,  # 1 Minute
        description="Time in milliseconds between automatic reconciliation cycles."
    )

    cooldown_seconds: float = Field(
        default=45.0,
        description=(
            "Grace period (in seconds) to ignore an Order ID after receiving a "
            "WebSocket event for it. Prevents race conditions between real-time "
            "pushes and the polling reconciler."
        )
    )

    @field_validator('sync_interval_ms')
    def interval_must_be_reasonable(cls, v):
        if v < 1000:
            raise ValueError('Sync interval must be at least 1000ms (1 second)')
        return v

    @field_validator('cooldown_seconds')
    def cooldown_must_be_positive(cls, v):
        if v < 0:
            raise ValueError('Cooldown seconds must be non-negative')
        return v


class FarmaxConfig(BaseModel):
    host: str = Field(..., description="Address that host the Firebird.")
    file: str = Field(..., description="Firebird database file.")
    user: str = Field(..., description="Firebird user.")
    password: str = Field(..., description="Firebird password.")


class ApiConfig(BaseModel):
    velide_server: str = Field(description="API URL to make requests to.")
    use_neighbourhood: Optional[bool] = Field(
        default=False, description="Enables sending neighbourhood details."
    )
    use_ssl: Optional[bool] = Field(
        default=True, description="Enables SSL verification on API requests."
    )
    timeout: Optional[float] = Field(default=15.0, description="API requests timeout.")

    velide_websockets_server: str = Field(
        description="Websockets server to receive updates from."
    )


class AuthenticationConfig(BaseModel):
    """
    A model to hold essential details for OAuth 2.0 and OIDC authentication flows.
    """

    domain: str = Field(
        ...,
        description="The domain of your authorization server. " \
        "This is the endpoint that will handle authentication.",
        examples=["your-tenant.auth0.com"],
    )

    client_id: str = Field(
        ...,
        description="The unique public identifier for your application, " \
        "assigned by the authorization server.",
    )

    scope: str = Field(
        ...,
        description="A space-separated list of permissions (scopes) " \
        "that the application is requesting.",
        examples=["openid profile email read:messages"],
    )

    audience: str = Field(
        ...,
        description="The unique identifier for the API that " \
        "your application wants to access (the resource server).",
    )


class Settings(BaseModel):
    target_system: TargetSystem = Field(
        default=TargetSystem.FARMAX, description="Application to integrate with."
    )
    auth: AuthenticationConfig
    api: ApiConfig
    farmax: Optional[FarmaxConfig] = None
    reconciliation: ReconciliationConfig = Field(
        default_factory=ReconciliationConfig,
        description="Settings for the reconciliation service."
    )
    sqlite_path: str = Field(
        default="resources/velide.db",
        description="Relative path to SQLite database file.",
    )
    sqlite_days_retention: int = Field(
        default=30,
        description=(
            "How many days will deliveries data last "
            "until it is cleaned by the Daily Cleanup Service."
        )
    )
    folder_to_watch: Optional[str] = Field(
        default=None, description="Used to listen for new files when using CDS."
    )

    @classmethod
    def from_yaml(cls: Type[T], path: str) -> T:
        """
        Loads configuration from a YAML file.

        Args:
            path: The path to the YAML configuration file.

        Returns:
            An instance of the AppConfig class.
        """
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")

        with open(config_path, "r", encoding="utf-8") as f:
            # Load YAML file into a Python dictionary
            config_data = yaml.safe_load(f)
            if not isinstance(config_data, dict):
                raise TypeError(
                    "Não foi possível converter o conteúdo do arquivo de configuração."
                )

            # Create an instance of the class (cls) using the loaded data
            return cls(**config_data)


config_path = os.path.join(BUNDLE_DIR, "resources", "config.yml")
config = Settings.from_yaml(config_path)
