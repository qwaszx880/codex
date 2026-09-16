from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://platform:platform@postgres:5432/platform"
    rabbitmq_url: str = "amqp://platform:platform@rabbitmq:5672//"
    oidc_issuer: str = "https://identity.example.invalid/realms/platform"
    oidc_audience: str = "cluster-platform"
    oidc_jwks_url: str | None = None
    auth_disabled: bool = False
    outbox_batch_size: int = 100
    management_cluster: str = "local-mgmt"

@lru_cache
def get_settings() -> Settings:
    return Settings()
