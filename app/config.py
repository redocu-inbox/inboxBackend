"""Load settings from environment variables and an optional .env file.

Pydantic Settings reads each field from an environment variable with the same
name in uppercase (database_url -> DATABASE_URL). Real environment variables
win over the .env file, which wins over the defaults below.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me"

    # `postgresql+psycopg` means "Postgres, using the psycopg v3 driver".
    database_url: str = "postgresql+psycopg://inbox:inbox@localhost:5432/inbox"
    redis_url: str = "redis://localhost:6379/0"

    # Comma-separated in .env. A plain string is easier to get right than a JSON list.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    storage_backend: str = "local"
    storage_local_path: str = "./storage"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it. Tests can call get_settings.cache_clear()."""
    return Settings()
