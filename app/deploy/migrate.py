"""Apply Alembic migrations while holding a Postgres advisory lock.

The lock is session-level, so the connection that takes it stays open until
upgrade finishes. A second process blocks on the same lock instead of racing
the schema. This is the only supported automatic migration path. The API and
the worker do not migrate when they start.
"""

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import get_settings

logger = logging.getLogger(__name__)

# Stable key shared by every process of this application. Not a secret.
ADVISORY_LOCK_KEY = 4815162342


def try_advisory_lock(connection) -> bool:
    """Return True when this connection acquired the migration lock."""
    row = connection.execute(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": ADVISORY_LOCK_KEY},
    ).scalar()
    return bool(row)


def unlock_advisory(connection) -> None:
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})


def upgrade_head_with_lock() -> None:
    """Upgrade to Alembic head. Wait if another process already holds the lock."""
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    try:
        logger.info("Waiting for the migration lock")
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        connection.commit()
        logger.info("Applying Alembic migrations")
        command.upgrade(Config("alembic.ini"), "head")
        logger.info("Migrations finished")
    finally:
        try:
            unlock_advisory(connection)
            connection.commit()
        except Exception:
            logger.warning("Could not release the migration lock before disconnect", exc_info=True)
        connection.close()
        engine.dispose()
