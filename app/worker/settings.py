"""ARQ worker configuration.

Start it with:
    arq app.worker.settings.WorkerSettings

ARQ imports this class, connects to Redis, and waits for jobs whose names match
the functions listed below (the function name `heartbeat` is the job name).
"""

import logging

from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_config import setup_logging
from app.worker.tasks import heartbeat

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    setup_logging(get_settings().log_level)
    logger.info("ARQ worker started")


async def shutdown(ctx: dict) -> None:
    logger.info("ARQ worker stopping")


class WorkerSettings:
    functions = [heartbeat]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
