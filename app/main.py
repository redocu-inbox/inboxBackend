"""FastAPI application entrypoint.

Uvicorn loads this module and looks for the object named `app`:
    uvicorn app.main:app
"""

import logging
from contextlib import asynccontextmanager

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
    yield
    engine.dispose()
    logger.info("API shut down")


app = FastAPI(title="Inbox API", version="0.1.0", lifespan=lifespan)

# Browsers block a Vite app on :5173 from calling an API on :8000 unless the
# API explicitly allows that origin. This middleware adds those headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
