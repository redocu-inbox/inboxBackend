"""Run the tests against a real Postgres, in a database separate from dev data.

The environment is set before anything imports app.*, because app.db builds its
engine from the settings at import time.
"""

import os
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, make_url, text

_TEST_DATABASE = "inbox_test"

_base_url = make_url(
    os.environ.get("DATABASE_URL", "postgresql+psycopg://inbox:inbox@localhost:5433/inbox")
)
_test_url = _base_url.set(database=_TEST_DATABASE)

os.environ["DATABASE_URL"] = _test_url.render_as_string(hide_password=False)
os.environ["APP_ENV"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-hs256"
os.environ["TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    """Recreate the test database and build its schema from the real migrations."""
    admin = create_engine(_base_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {_TEST_DATABASE}"))
    admin.dispose()
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    yield
    with engine.begin() as connection:
        # Every other table references users with ON DELETE CASCADE.
        connection.execute(text("TRUNCATE users CASCADE"))


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """A client without lifespan, so the tests need no Redis. Uploads go to tmp_path."""
    from app.storage.local import LocalStorage

    monkeypatch.setattr("app.services.forms.pipeline.get_storage", lambda: LocalStorage(str(tmp_path)))
    app.state.arq = AsyncMock()
    return TestClient(app)


@pytest.fixture
def register_user(client: TestClient) -> Callable[[str], dict[str, str]]:
    """Register an account and return the headers that authenticate as it."""

    def _register(email: str) -> dict[str, str]:
        response = client.post(
            "/auth/register", json={"email": email, "password": "correct-horse-battery"}
        )
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _register
