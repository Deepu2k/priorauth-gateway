"""Settings, read from environment variables. Secrets never live in the repo."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "priorauth-api"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://priorauth:priorauth@localhost:5432/priorauth"
    amqp_url: str = "amqp://guest:guest@localhost:5672/%2F"

    payer_base_url: str = "http://localhost:8081"
    payer_timeout_seconds: float = 2.0
    fhir_base_url: str = "https://hapi.fhir.org/baseR4"
    fhir_timeout_seconds: float = 5.0

    # Retry policy: attempt 1, 2, ... max_attempts, then the dead letter queue.
    max_attempts: int = 3
    retry_delay_ms: int = 5000

    # Auth. JWT_SECRET has no default on purpose: the service refuses to start without it.
    jwt_secret: str = Field(min_length=32)
    jwt_issuer: str = "priorauth-gateway"
    jwt_audience: str = "priorauth-api"
    jwt_ttl_minutes: int = 30
    # Demo users for the token endpoint: "username:sha256(password):role1|role2,..."
    demo_users: str = ""
    # Partner system API keys: "name:sha256(key):role1|role2,..."
    api_keys: str = ""

    worker_metrics_port: int = 9100


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
