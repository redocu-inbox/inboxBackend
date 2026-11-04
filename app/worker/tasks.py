"""Background jobs.

ARQ is asyncio-based, so every job is an `async def`. The first argument, `ctx`,
is a dict ARQ passes in (Redis pool, job id, and anything we add at startup).
Further arguments are strings, because ARQ stores them in Redis.
"""
import json
import logging
from uuid import UUID
from datetime import datetime
from urllib.request import Request, urlopen

from sqlmodel import Session, select

from app.db import engine
from app.models.form_fill_job import FormFillJob, FormFillStatus
from app.models.user import utcnow
from app.models.connected_account import ConnectedAccount
from app.services.graph.tokens import refresh_access_token, needs_refresh
from app.models.message import Message

_MESSAGES_URL = (
    "https://graph.microsoft.com/v1.0/me/messages"
    "?$select=id,subject,bodyPreview,receivedDateTime"
    "&$top=25&$orderby=receivedDateTime%20desc"
)

def _load_account(session: Session, account_or_subscription_id: str | None) -> ConnectedAccount | None:
    """Find the mailbox by a graoh subscription id, or by its own id."""
    account = session.exec(
        select(ConnectedAccount).where(
            ConnectedAccount.graph_subscription_id == account_or_subscription_id
        )
    ).first()
    if account is not None:
        return account
    try:
        account_id = UUID(account_or_subscription_id)
    except ValueError:
        return None
    return session.get(ConnectedAccount, account_id)

def _graph_get(url: str, access_token: str) -> dict:
    """GET one graph url. The token is a header and must not be logged."""
    request = Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urlopen(request,timeout=30) as response:
        return json.load(response)

def _upsert_message(session: Session, account: ConnectedAccount, item: dict) -> None:
    """Insert a message or update it when this mailbox already stored that graph id."""
    graph_id = item.get("id")
    recieved_raw = item.get("receivedDateTime")
    if not graph_id or not recieved_raw:
        return
    received_at = datetime.fromisoformat(recieved_raw.replace("Z", "+00:00"))
    existing = session.exec(
        select(Message).where(
            Message.connected_account_id == account.id,
            Message.graph_id == graph_id,
        )
    ).first()
    if existing is None:
        existing = Message(
            user_id=account.user_id,
            connected_account_id=account.id,
            graph_id=graph_id,
            subject=item.get("subject"),
            body_preview=item.get("bodyPreview"),
            received_at=received_at,
        )
    else:
        existing.subject = item.get("subject")
        existing.body_preview = item.get("bodyPreview")
        existing.received_at = received_at
        existing.updated_at = utcnow()
    session.add(existing)


logger = logging.getLogger(__name__)


async def heartbeat(ctx: dict) -> str:
    """Sample job so the worker has a registered function. Nothing calls it yet."""
    logger.info("heartbeat job ran (job_id=%s)", ctx.get("job_id"))
    return "ok"


async def sync_mailbox_delta(ctx: dict, subscription_id: str) -> None:
    """Load one mailbox, refresh its token when needed, and store its messages."""
    with Session(engine) as session:
        account = _load_account(session, subscription_id)
        if account is None:
            logger.warning("No connected account for %s", subscription_id)
            return
        if needs_refresh(account):
            account = refresh_access_token(session, account)

        url = _MESSAGES_URL 
        pages = 0
        while url and pages < 4:
            payload = _graph_get(url, account.access_token)
            for item in payload .get("value", []):
                _upsert_message(session, account, item)
            session.commit()
            pages += 1
            url = payload.get("@odata.nextLink")
    logger.info(
        "sync_mailbox_delta subscription_id=%s job_id=%s",
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
