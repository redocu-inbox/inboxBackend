"""Load settings from environment variables and an optional .env file.

Pydantic Settings reads each field from an environment variable with the same
name in uppercase (database_url -> DATABASE_URL). Real environment variables
win over the .env file, which wins over the defaults below.
"""

from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRET_KEYS = {"change-me", "change-me-to-a-long-random-string"}
_MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    # Signs login JWTs. Changing it logs everyone out.
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 60
    # Fernet key that encrypts Microsoft tokens at rest. Changing it makes stored
    # tokens unreadable, so it is separate from secret_key.
    token_encryption_key: str = ""

    # `postgresql+psycopg` means "Postgres, using the psycopg v3 driver".
    database_url: str = "postgresql+psycopg://inbox:inbox@localhost:5432/inbox"
    redis_url: str = "redis://localhost:6379/0"

    # Comma-separated in .env. A plain string is easier to get right than a JSON list.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    storage_backend: str = "local"
    storage_local_path: str = "./storage"

    # Microsoft Graph. Unused until OAuth and delta sync are implemented.
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_tenant_id: str = "common"
    graph_redirect_uri: str = ""
    # Shared secret Graph echoes on each webhook notification.
    graph_webhook_client_state: str = ""

    graph_scopes: str = (
        "https://graph.microsoft.com/User.Read,"
        "https://graph.microsoft.com/Mail.Read,"
        "https://graph.microsoft.com/Mail.ReadWrite"
    )

    # stub for now. Later values: openai, anthropic.
    llm_provider: str = "stub"
    llm_model: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        """Refuse to start with a malformed key, or with dev secrets outside development."""
        if self.token_encryption_key:
            try:
                Fernet(self.token_encryption_key)
            except ValueError as exc:
                raise ValueError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc

        if self.app_env == "development":
            return self
        if self.secret_key in _PLACEHOLDER_SECRET_KEYS or len(self.secret_key) < _MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be a random string of at least {_MIN_SECRET_KEY_LENGTH} "
                f"characters when APP_ENV is {self.app_env!r}"
            )
        if not self.token_encryption_key:
            raise ValueError(f"TOKEN_ENCRYPTION_KEY is required when APP_ENV is {self.app_env!r}")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def graph_scope_list(self) -> list[str]:
        return [scope.strip() for scope in self.graph_scopes.split(",") if scope.strip()]

    @property
    def graph_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.graph_tenant_id}"

@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it. Tests can call get_settings.cache_clear()."""
    return Settings()
