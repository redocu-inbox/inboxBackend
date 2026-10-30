"""Background jobs.

ARQ is asyncio-based, so every job is an `async def`. The first argument, `ctx`,
is a dict ARQ passes in (Redis pool, job id, and anything we add at startup).
Further arguments are strings, because ARQ stores them in Redis.
"""

import logging
from uuid import UUID

from sqlmodel import Session

from app.db import engine
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.user import utcnow

logger = logging.getLogger(__name__)


async def heartbeat(ctx: dict) -> str:
    """Sample job so the worker has a registered function. Nothing calls it yet."""
    logger.info("heartbeat job ran (job_id=%s)", ctx.get("job_id"))
    return "ok"


async def sync_mailbox_delta(ctx: dict, subscription_id: str) -> None:
    """TODO: find the account by graph_subscription_id, call GraphClient.sync_inbox_delta,
    and upsert Message rows.
    """
    logger.info(
        "TODO sync_mailbox_delta subscription_id=%s job_id=%s",
        subscription_id,
        ctx.get("job_id"),
    )


async def match_form_template(ctx: dict, job_id: str) -> None:
    """Mark the fill job running.

    TODO: call get_llm() to match a template, save extracted_values,
    then enqueue run_form_fill.
    """
    with Session(engine) as session:
        job = session.get(FormFillJob, UUID(job_id))
        if job is None:
            logger.warning("fill job %s was not found", job_id)
            return
        job.status = FormFillStatus.running
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
    logger.info(
        "TODO match_form_template fill_job=%s job_id=%s",
        job_id,
        ctx.get("job_id"),
    )


async def run_form_fill(ctx: dict, job_id: str) -> None:
    """TODO: load template bytes, call get_filler(...).fill, store the result,
    set filled_file_path and status.
    """
    logger.info(
        "TODO run_form_fill fill_job=%s job_id=%s",
        job_id,
        ctx.get("job_id"),
    )


async def renew_graph_subscriptions(ctx: dict) -> None:
    """TODO: renew subscriptions that expire within a day."""
    logger.info("TODO renew_graph_subscriptions job_id=%s", ctx.get("job_id"))
