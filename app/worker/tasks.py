"""Jobs that are neither mailbox work nor form fills.

Mailbox jobs live in mail_tasks. Form jobs live in form_tasks. ARQ job names
are the function names. The first argument, `ctx`, is the dict ARQ passes in.
Further arguments are strings.
"""

import asyncio
import logging

from sqlmodel import Session

from app.db import engine
from app.services.account_deletion import purge_pending_accounts

logger = logging.getLogger(__name__)


async def purge_deleted_accounts(ctx: dict) -> None:
    """Process durable account deletion requests using the worker's storage."""
    await asyncio.to_thread(purge_pending_accounts)


async def send_password_reset(ctx: dict, user_id: str, request_id: str) -> None:
    """Deliver one reset email. Arguments are ids only. The code is created here."""
    from uuid import UUID

    from app.services.auth.password_reset import deliver_password_reset

    del ctx
    with Session(engine) as session:
        deliver_password_reset(session, UUID(user_id), UUID(request_id))


async def heartbeat(ctx: dict) -> str:
    """Sample job so the worker has a registered function. Nothing calls it yet."""
    logger.info("heartbeat job ran (job_id=%s)", ctx.get("job_id"))
    return "ok"
