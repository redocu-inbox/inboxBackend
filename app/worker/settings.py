"""ARQ worker configuration.

Start it with:
    arq app.worker.settings.WorkerSettings

ARQ imports this class, connects to Redis, and waits for jobs whose names match
the functions listed below (the function name `heartbeat` is the job name).
"""

import logging

from arq import cron, func
from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_config import setup_logging
from app.worker.form_tasks import match_form_template, run_form_fill
from app.worker.mail_tasks import (
    generate_reply_draft,
    renew_graph_subscriptions,
    rescore_connected_mailboxes,
    rescore_mailbox_priority,
    sync_connected_mailboxes,
    sync_mailbox_delta,
)
from app.worker.tasks import heartbeat, purge_deleted_accounts, send_password_reset

logger = logging.getLogger(__name__)


def mailbox_sync_minutes(interval: int) -> set[int]:
    """Minute marks for the periodic sync. 60 means once an hour, at minute 0."""
    valid = isinstance(interval, int) and not isinstance(interval, bool)
    step = interval if valid and 1 <= interval <= 60 else 5
    return set(range(0, 60, step))


def _cron_jobs() -> list:
    settings = get_settings()
    jobs = [cron(renew_graph_subscriptions, hour={0, 12}, minute=0, max_tries=3)]
    if settings.periodic_mailbox_sync:
        jobs.append(
            cron(
                sync_connected_mailboxes,
                minute=mailbox_sync_minutes(settings.mailbox_sync_interval_minutes),
                unique=True,
            )
        )
    jobs.append(cron(purge_deleted_accounts, run_at_startup=True))
    return jobs


async def startup(ctx: dict) -> None:
    setup_logging(get_settings().log_level)
    logger.info("ARQ worker started")
    # Direct call, not enqueue: on_startup runs before this worker polls, and
    # the summary line has to include how many stored messages were rescored.
    try:
        await rescore_connected_mailboxes(ctx)
    except Exception as exc:
        logger.warning(
            "priority rescore on startup failed error_type=%s",
            type(exc).__name__,
        )


async def shutdown(ctx: dict) -> None:
    logger.info("ARQ worker stopping")


class WorkerSettings:
    functions = [
        heartbeat,
        generate_reply_draft,
        sync_mailbox_delta,
        rescore_mailbox_priority,
        send_password_reset,
        func(match_form_template, max_tries=3),
        func(run_form_fill, max_tries=3),
        renew_graph_subscriptions,
    ]
    cron_jobs = _cron_jobs()
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    on_shutdown = shutdown
