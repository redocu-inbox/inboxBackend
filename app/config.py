"""Load settings from environment variables and an optional .env file.

Pydantic Settings reads each field from an environment variable with the same
name in uppercase (database_url -> DATABASE_URL). Real environment variables
win over the .env file, which wins over the defaults below.
"""

from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url

_PLACEHOLDER_SECRET_KEYS = {"change-me", "change-me-to-a-long-random-string"}
_MIN_SECRET_KEY_LENGTH = 32
_LOCAL_DB_PASSWORD = "inbox"
_ACCEPTED_SSLMODES = {"require", "verify-ca", "verify-full"}
_GRAPH_CALLBACK_PATH = "/auth/microsoft/callback"
_GOOGLE_CALLBACK_PATH = "/auth/google/callback"
_SLACK_CALLBACK_PATH = "/auth/slack/callback"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    # The worker rescores stored mail for every connected mailbox on startup.
    # Set RESCORE_ON_STARTUP=0 to skip. The pass does not call Graph, Gmail, or an LLM.
    rescore_on_startup: bool = True
    # Civil dates for "today", weekdays, and due/overdue. Default is US Eastern.
    scoring_timezone: str = "America/New_York"
    # Periodic incremental sync of every connected Outlook and Gmail mailbox.
    # Set PERIODIC_MAILBOX_SYNC=0 to skip. The free Render service sleeps when
    # idle, so the cron runs only while that process is awake.
    periodic_mailbox_sync: bool = True
    # Minutes between syncs. 1 through 60. Anything else uses 5.
    mailbox_sync_interval_minutes: int = 5
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

    # Microsoft Graph app used to connect a mailbox and list recent messages.
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_tenant_id: str = "common"
    graph_redirect_uri: str = ""
    # Shared secret Graph echoes on each webhook notification.
    graph_webhook_client_state: str = ""
    # Public URL Graph calls when mail changes. Empty skips creating a subscription.
    graph_webhook_url: str = ""

    # Google OAuth2 app used to connect a mailbox and list recent messages.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    # Slack user token (not a bot token). Empty values leave Slack disabled.
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_redirect_uri: str = ""
    # Kill switch for chat.postMessage. Sync and read stay available when this is false.
    slack_send_enabled: bool = True

    graph_scopes: str = (
        "https://graph.microsoft.com/User.Read,"
        "https://graph.microsoft.com/Mail.Read,"
        "https://graph.microsoft.com/Mail.ReadWrite,"
        "https://graph.microsoft.com/Mail.Send"
    )

    # Vite origin, with no path. The Microsoft callback redirects to
    # {frontend_url}/inbox. Override with FRONTEND_URL.
    frontend_url: str = "http://localhost:5173"

    # When false, a mailbox sync does not enqueue form matching. Matching
    # starts from POST /forms/jobs. Turning this on is a later change.
    form_match_on_sync: bool = False

    # stub for now. Later values: openai, anthropic.
    llm_provider: str = "stub"
    llm_model: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # fake and console are local only. production must be resend or disabled.
    email_provider: str = "fake"
    email_api_key: str = ""
    email_from: str = ""
    # How many trusted proxies append X-Forwarded-For. 0 ignores that header.
    forwarded_for_hops: int = 0
    password_reset_limit: int = 5
    password_reset_window_seconds: int = 3600

    @field_validator("mailbox_sync_interval_minutes", mode="before")
    @classmethod
    def _sync_interval(cls, value: object) -> int:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 5
        if 1 <= number <= 60:
            return number
        return 5

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        """Refuse to start with a malformed key, or with dev secrets outside development."""
        if self.token_encryption_key:
            try:
                Fernet(self.token_encryption_key)
            except ValueError as exc:
                raise ValueError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc

        self._check_frontend_url()
        if self.app_env == "development":
            return self
        if self.secret_key in _PLACEHOLDER_SECRET_KEYS or len(self.secret_key) < _MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be a random string of at least {_MIN_SECRET_KEY_LENGTH} "
                f"characters when APP_ENV is {self.app_env!r}"
            )
        if not self.token_encryption_key:
            raise ValueError(f"TOKEN_ENCRYPTION_KEY is required when APP_ENV is {self.app_env!r}")
        if self.app_env == "production":
            self._check_production_deployment()
        return self

    def _check_frontend_url(self) -> None:
        """Reject anything that is not a single origin. The callback redirects here."""
        raw = self.frontend_url
        lowered = raw.lower()
        if raw != raw.strip() or any(char in raw for char in "\r\n\t "):
            raise ValueError("FRONTEND_URL must be an http(s) origin with no whitespace")
        if "%0d" in lowered or "%0a" in lowered or "\\" in raw:
            raise ValueError("FRONTEND_URL must not contain encoded line breaks or backslashes")
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("FRONTEND_URL must be an absolute http(s) origin")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("FRONTEND_URL must be an origin with no userinfo, query, or fragment")
        if parts.path not in {"", "/"}:
            raise ValueError("FRONTEND_URL must not include a path")

    def _check_production_deployment(self) -> None:
        """Refuse a public boot that still has local defaults or a cleartext database.

        These checks run only when APP_ENV is production. Local Compose and the
        test suite stay on development and test.
        """
        frontend = _normalize_origin(self.frontend_url, what="FRONTEND_URL")
        if not frontend.startswith("https://"):
            raise ValueError("FRONTEND_URL must use https when APP_ENV is production")
        if not self.cors_origins.strip() or "*" in self.cors_origin_list:
            raise ValueError("CORS_ORIGINS must be an explicit https origin list when APP_ENV is production")
        origins = [_normalize_origin(item, what="CORS_ORIGINS") for item in self.cors_origin_list]
        if any(not origin.startswith("https://") for origin in origins):
            raise ValueError("CORS_ORIGINS must use https when APP_ENV is production")
        if frontend not in origins:
            raise ValueError("CORS_ORIGINS must include FRONTEND_URL when APP_ENV is production")
        _check_production_database(self.database_url)
        _check_production_redis(self.redis_url)
        _check_production_redirect(
            self.graph_redirect_uri,
            setting_name="GRAPH_REDIRECT_URI",
            expected_path=_GRAPH_CALLBACK_PATH,
        )
        _check_production_redirect(
            self.google_redirect_uri,
            setting_name="GOOGLE_REDIRECT_URI",
            expected_path=_GOOGLE_CALLBACK_PATH,
        )
        # Slack is optional. An empty redirect must not stop the process from booting.
        if self.slack_redirect_uri.strip():
            _check_production_redirect(
                self.slack_redirect_uri.strip(),
                setting_name="SLACK_REDIRECT_URI",
                expected_path=_SLACK_CALLBACK_PATH,
            )
        if len(self.graph_webhook_client_state) < 16:
            raise ValueError(
                "GRAPH_WEBHOOK_CLIENT_STATE must be a random string of at least 16 characters "
                "when APP_ENV is production"
            )
        if self.frontend_url.endswith("/") or any(origin.endswith("/") for origin in self.cors_origin_list):
            raise ValueError("FRONTEND_URL and CORS_ORIGINS must not have a trailing slash in production")
        self._check_production_email()

    def _check_production_email(self) -> None:
        provider = self.email_provider.strip().lower()
        if provider in {"", "fake", "console"}:
            raise ValueError(
                "EMAIL_PROVIDER must be resend or disabled when APP_ENV is production. "
                "fake and console must not report that a reset email was sent."
            )
        if provider == "disabled":
            return
        if provider != "resend":
            raise ValueError("EMAIL_PROVIDER must be resend or disabled when APP_ENV is production")
        if not self.email_api_key or not self.email_from:
            raise ValueError("EMAIL_API_KEY and EMAIL_FROM are required when EMAIL_PROVIDER is resend")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def graph_scope_list(self) -> list[str]:
        return [scope.strip() for scope in self.graph_scopes.split(",") if scope.strip()]

    @property
    def graph_authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.graph_tenant_id}"

def _normalize_origin(value: str, *, what: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"{what} must be an absolute http(s) origin")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError(f"{what} must be an origin with no path, query, or userinfo")
    if parts.port:
        return f"{parts.scheme}://{parts.hostname}:{parts.port}"
    return f"{parts.scheme}://{parts.hostname}"


def _check_production_database(database_url: str) -> None:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError("DATABASE_URL is not a valid database URL") from exc
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver when APP_ENV is production")
    if not url.password or url.password == _LOCAL_DB_PASSWORD:
        raise ValueError("DATABASE_URL must not use the local example password when APP_ENV is production")
    host = (url.host or "").lower()
    if "-pooler" in host or url.query.get("pgbouncer") == "true":
        raise ValueError(
            "DATABASE_URL must be a direct Postgres host when APP_ENV is production. "
            "A transaction pooler breaks migrations and advisory locks."
        )
    sslmode = url.query.get("sslmode")
    if sslmode not in _ACCEPTED_SSLMODES:
        raise ValueError(
            "DATABASE_URL must set sslmode=require, verify-ca, or verify-full when APP_ENV is production"
        )


def _check_production_redis(redis_url: str) -> None:
    parts = urlsplit(redis_url)
    if parts.scheme not in {"redis", "rediss"}:
        raise ValueError("REDIS_URL must use redis:// or rediss:// when APP_ENV is production")
    if not parts.password:
        raise ValueError("REDIS_URL must include a password when APP_ENV is production")


def _check_production_redirect(redirect_uri: str, *, setting_name: str, expected_path: str) -> None:
    parts = urlsplit(redirect_uri)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{setting_name} must be an https URL when APP_ENV is production")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{setting_name} must not include userinfo, a query, or a fragment")
    if parts.path.rstrip("/") != expected_path:
        raise ValueError(f"{setting_name} path must be {expected_path}")


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it. Tests can call get_settings.cache_clear()."""
    return Settings()
