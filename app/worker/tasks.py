"""Background jobs.

ARQ is asyncio-based, so every job is an `async def`. The first argument, `ctx`,
is a dict ARQ passes in (Redis pool, job id, and anything we add at startup).
"""

import logging

logger = logging.getLogger(__name__)


async def heartbeat(ctx: dict) -> str:
    """Sample job so the worker has a registered function. Nothing calls it yet."""
    logger.info("heartbeat job ran (job_id=%s)", ctx.get("job_id"))
    return "ok"
