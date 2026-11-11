"""Deployment settings, Redis URL parsing, the migration lock, and the supervisor.

No test connects to Neon, Render, or Microsoft.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from arq.connections import RedisSettings
from pydantic import ValidationError
from app.config import Settings, get_settings
from app.db import engine
from app.deploy.migrate import try_advisory_lock, unlock_advisory
from app.deploy.supervise import run_processes

_FERNET = get_settings().token_encryption_key


def _production(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "secret_key": "p" * 32,
        "token_encryption_key": _FERNET,
        "database_url": (
            "postgresql+psycopg://app:s3cret@ep-example.aws.neon.tech/inbox?sslmode=require"
        ),
        "redis_url": "rediss://:s3cret@kv.internal:6379/0",
        "frontend_url": "https://redocu.app",
        "cors_origins": "https://redocu.app",
        "graph_redirect_uri": "https://inbox-api.onrender.com/auth/microsoft/callback",
        "google_redirect_uri": "https://inbox-api.onrender.com/auth/google/callback",
        "graph_webhook_client_state": "w" * 32,
        "email_provider": "disabled",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_settings_accept_direct_tls_postgres_and_rediss():
    settings = _production()
    assert settings.frontend_url == "https://redocu.app"
    parsed = RedisSettings.from_dsn(settings.redis_url)
    assert parsed.ssl is True
    assert parsed.password == "s3cret"
    assert parsed.host == "kv.internal"
    assert parsed.database == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"frontend_url": "http://localhost:5173", "cors_origins": "http://localhost:5173"},
        {"cors_origins": "*"},
        {"cors_origins": "https://example.com"},
        {
            "database_url": (
                "postgresql+psycopg://inbox:inbox@ep-example.aws.neon.tech/inbox?sslmode=require"
            )
        },
        {
            "database_url": (
                "postgresql+psycopg://app:s3cret@ep-example-pooler.aws.neon.tech/inbox?sslmode=require"
            )
        },
        {"database_url": "postgresql+psycopg://app:s3cret@ep-example.aws.neon.tech/inbox"},
        {"database_url": "postgresql://app:s3cret@ep-example.aws.neon.tech/inbox?sslmode=require"},
        {"redis_url": "redis://kv.internal:6379/0"},
        {"graph_redirect_uri": ""},
        {"graph_redirect_uri": "http://inbox-api.onrender.com/auth/microsoft/callback"},
        {"graph_redirect_uri": "https://inbox-api.onrender.com/callback"},
        {"graph_redirect_uri": "https://inbox-api.onrender.com/auth/microsoft/callback?code=1"},
        {"graph_redirect_uri": "https://user:pass@inbox-api.onrender.com/auth/microsoft/callback"},
        {"google_redirect_uri": ""},
        {"google_redirect_uri": "http://inbox-api.onrender.com/auth/google/callback"},
        {"google_redirect_uri": "https://inbox-api.onrender.com/callback"},
        {"google_redirect_uri": "https://inbox-api.onrender.com/auth/google/callback?code=1"},
        {"google_redirect_uri": "https://user:pass@inbox-api.onrender.com/auth/google/callback"},
        {"graph_webhook_client_state": "short"},
        {"frontend_url": "https://redocu.app/", "cors_origins": "https://redocu.app/"},
        {"email_provider": "fake"},
        {"email_provider": "console"},
        {"email_provider": "resend", "email_api_key": "", "email_from": ""},
    ],
)
def test_production_settings_reject_unsafe_defaults(overrides):
    with pytest.raises(ValidationError):
        _production(**overrides)


def test_development_still_allows_local_defaults():
    Settings(
        _env_file=None,
        app_env="development",
        secret_key="change-me",
        token_encryption_key="",
        database_url="postgresql+psycopg://inbox:inbox@postgres:5432/inbox",
        redis_url="redis://redis:6379/0",
        frontend_url="http://localhost:5173",
        cors_origins="http://localhost:5173,http://127.0.0.1:5173",
        graph_redirect_uri="",
        graph_webhook_client_state="",
    )


def test_migration_lock_is_exclusive():
    first = engine.connect()
    second = engine.connect()
    try:
        assert try_advisory_lock(first) is True
        first.commit()
        assert try_advisory_lock(second) is False
        second.rollback()
        unlock_advisory(first)
        first.commit()
        assert try_advisory_lock(second) is True
        unlock_advisory(second)
        second.commit()
        assert try_advisory_lock(first) is True
        unlock_advisory(first)
        first.commit()
    finally:
        first.close()
        second.close()


def test_supervisor_stops_the_other_child_when_one_exits():
    code = run_processes(
        [
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            [sys.executable, "-c", "import time; time.sleep(30)"],
        ]
    )
    assert code == 1


def test_supervisor_reaps_children_on_sigterm(tmp_path: Path):
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    child = (
        "import os, sys, time, pathlib; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    program = (
        "import sys\n"
        "from app.deploy.supervise import run_processes\n"
        "raise SystemExit(run_processes([\n"
        f"    [sys.executable, '-c', {child!r}, str(sys.argv[1])],\n"
        f"    [sys.executable, '-c', {child!r}, str(sys.argv[2])],\n"
        "]))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(pid_dir / "a"), str(pid_dir / "b")],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ((pid_dir / "a").exists() and (pid_dir / "b").exists()):
        time.sleep(0.05)
    pids = [int((pid_dir / name).read_text()) for name in ("a", "b")]
    os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=10) == 0
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
