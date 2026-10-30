"""ARQ worker configuration.

Start it with:
    arq app.worker.settings.WorkerSettings

ARQ imports this class, connects to Redis, and waits for jobs whose names match
the functions listed below (the function name `heartbeat` is the job name).
"""

import logging

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_config import setup_logging
from app.worker.tasks import (
    heartbeat,
    match_form_template,
    renew_graph_subscriptions,
    run_form_fill,
    sync_mailbox_delta,
)

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    setup_logging(get_settings().log_level)
    logger.info("ARQ worker started")


async def shutdown(ctx: dict) -> None:
    logger.info("ARQ worker stopping")


class WorkerSettings:
    functions = [
        heartbeat,
        sync_mailbox_delta,
        match_form_template,
        run_form_fill,
        renew_graph_subscriptions,
    ]
    cron_jobs = [cron(renew_graph_subscriptions, hour={0, 12}, minute=0)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
