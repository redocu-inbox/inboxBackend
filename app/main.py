"""FastAPI application entrypoint.

Uvicorn loads this module and looks for the object named `app`:
    uvicorn app.main:app
"""

import logging
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.db import engine
from app.logging_config import setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Code before `yield` runs on startup. Code after it runs on shutdown."""
    logger.info("API starting (env=%s)", settings.app_env)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    logger.info("ARQ pool ready")
    try:
        yield
    finally:
        await app.state.arq.close()
        engine.dispose()
        logger.info("API shut down")


app = FastAPI(title="Inbox API", version="0.1.0", lifespan=lifespan)

# Browsers block a Vite app on :5173 from calling an API on :8000 unless the
# API explicitly allows that origin. This middleware adds those headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    # Browsers hide this from the Vite app unless it is listed here.
    expose_headers=["X-Total-Count"],
)

app.include_router(api_router)
