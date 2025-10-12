from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class TargetSystem(str, Enum):
    CDS = "CDS"
    FARMAX = "Farmax"

class ApiConfig(BaseModel):
    velide_server: str = Field(description="API URL to make requests to.")
    use_neighbourhood: Optional[bool] = Field(default=False, description="Enables sending neighbourhood details.")
    use_ssl: Optional[bool] = Field(default=True, description="Enables SSL verification on API requests.")
    timeout: Optional[float] = Field(default=15.0, description="API requests timeout.")

class AuthenticationConfig(BaseModel):
    """
    A model to hold essential details for OAuth 2.0 and OIDC authentication flows.
    """
    domain: str = Field(
        ...,
        description="The domain of your authorization server. This is the endpoint that will handle authentication.",
        examples=["your-tenant.auth0.com"]
    )
    
    client_id: str = Field(
        ...,
        description="The unique public identifier for your application, assigned by the authorization server."
    )
    
    scope: str = Field(
        ...,
        description="A space-separated list of permissions (scopes) that the application is requesting.",
        examples=["openid profile email read:messages"]
    )
    
    audience: str = Field(
        ...,
        description="The unique identifier for the API that your application wants to access (the resource server)."
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__")

    target_system: Optional[TargetSystem] = Field(default=TargetSystem.FARMAX, description="Application to integrate with.")
    auth: AuthenticationConfig
    api: ApiConfig
    folder_to_watch: Optional[str] = Field(default=None, description="Used to listen for new files when using CDS.")


config = Settings()