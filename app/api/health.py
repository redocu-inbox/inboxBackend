"""GET /health — proves the API process and its dependencies are up."""

import logging

import redis
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter()


class HealthChecks(BaseModel):
    api: str
    database: str
    pgvector: str
    redis: str
    storage: str


class HealthResponse(BaseModel):
    status: str
    checks: HealthChecks


def _check_database_and_pgvector() -> tuple[str, str]:
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        row = session.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).first()
    if row is None:
        return "ok", "missing"
    return "ok", "ok"


def _check_redis() -> str:
    client = redis.Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    finally:
        client.close()
    return "ok"


def _check_storage() -> str:
    get_storage().ready()
    return "ok"


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    # A plain `def` (not `async def`) is intentional. The database and Redis
    # clients here are synchronous; FastAPI runs sync endpoints in a threadpool
    # so they don't block the event loop.
    checks = {
        "api": "ok",
        "database": "ok",
        "pgvector": "ok",
        "redis": "ok",
        "storage": "ok",
    }

    try:
        checks["database"], checks["pgvector"] = _check_database_and_pgvector()
    except Exception:
        logger.warning("Database health check failed", exc_info=True)
        checks["database"] = "error"
        checks["pgvector"] = "error"

    try:
        checks["redis"] = _check_redis()
    except Exception:
        logger.warning("Redis health check failed", exc_info=True)
        checks["redis"] = "error"

    try:
        checks["storage"] = _check_storage()
    except Exception:
        logger.warning("Storage health check failed", exc_info=True)
        checks["storage"] = "error"

    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if healthy else "degraded",
        checks=HealthChecks(**checks),
    )
