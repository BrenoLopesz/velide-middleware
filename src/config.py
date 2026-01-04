from enum import Enum
import os
from pathlib import Path
from typing import Optional, Type, TypeVar
from pydantic import BaseModel, Field
import yaml

from utils.bundle_dir import BUNDLE_DIR

T = TypeVar("T", bound="Settings")


class TargetSystem(str, Enum):
    CDS = "CDS"
    FARMAX = "Farmax"


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

    velide_websockets_server: Optional[str] = Field(
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
    sqlite_path: str = Field(
        default="resources/velide.db",
        description="Relative path to SQLite database file.",
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
