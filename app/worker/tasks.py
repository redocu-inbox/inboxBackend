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
from app.services.graph.html_text import to_plain_text
from app.services.graph.tokens import (
    MailboxNotWritable,
    TokenRefreshError,
    needs_refresh,
    refresh_access_token,
)
from app.services.mailboxes import GRAPH_UNAVAILABLE, TOKEN_REFRESH_FAILED
from app.models.message import Message

_GRAPH_HOST = "https://graph.microsoft.com/"
_MESSAGES_URL = (
    "https://graph.microsoft.com/v1.0/me/messages"
    "?$select=id,subject,bodyPreview,receivedDateTime,from,isRead,body"
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

def _plain_address(value: object) -> str | None:
    text = to_plain_text(value)
    if text is None or "@" not in text or any(char.isspace() for char in text):
        return None
    return text[:320]


def graph_message_fields(item: dict) -> dict | None:
    """Map one Graph message to stored columns. HTML never lands in body_text."""
    graph_id = item.get("id")
    received_raw = item.get("receivedDateTime")
    if not isinstance(graph_id, str) or not graph_id or not isinstance(received_raw, str):
        return None
    try:
        received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    sender = item.get("from") if isinstance(item.get("from"), dict) else {}
    email_address = sender.get("emailAddress") if isinstance(sender.get("emailAddress"), dict) else {}
    is_read = item.get("isRead")
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    content = body.get("content") if isinstance(body.get("content"), str) else None
    return {
        "graph_id": graph_id[:512],
        "subject": to_plain_text(item.get("subject")),
        "body_preview": to_plain_text(item.get("bodyPreview")),
        "received_at": received_at,
        "from_name": to_plain_text(email_address.get("name")),
        "from_email": _plain_address(email_address.get("address")),
        "is_read": is_read if isinstance(is_read, bool) else None,
        "body_text": to_plain_text(content),
    }


def _upsert_message(session: Session, account: ConnectedAccount, item: dict) -> None:
    """Insert a message or update it when this mailbox already stored that graph id."""
    fields = graph_message_fields(item)
    if fields is None:
        return
    existing = session.exec(
        select(Message).where(
            Message.connected_account_id == account.id,
            Message.graph_id == fields["graph_id"],
        )
    ).first()
    if existing is None:
        existing = Message(
            user_id=account.user_id,
            connected_account_id=account.id,
            **fields,
        )
    else:
        for key, value in fields.items():
            if key == "graph_id":
                continue
            setattr(existing, key, value)
        existing.updated_at = utcnow()
    session.add(existing)


logger = logging.getLogger(__name__)


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


def _generation(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _lock_account(session: Session, account_id: UUID) -> ConnectedAccount | None:
    # populate_existing: this session may already hold the row from before a
    # disconnect committed on another connection.
    return session.exec(
        select(ConnectedAccount)
        .where(ConnectedAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _stale(account: ConnectedAccount, generation: int | None) -> bool:
    """True when this job must not read Graph or write tokens or messages."""
    if account.disconnected_at is not None:
        return True
    if not account.access_token or not account.refresh_token:
        return True
    if generation is not None and int(account.sync_generation or 0) != generation:
        return True
    return False


def _mark_failed(session: Session, account_id: UUID, generation: int | None, error_code: str) -> None:
    """Record failure for this generation only. Do not move last_sync_at."""
    account = _lock_account(session, account_id)
    if account is None or account.disconnected_at is not None:
        session.rollback()
        return
    if generation is not None and int(account.sync_generation or 0) != generation:
        session.rollback()
        return
    account.sync_status = "failed"
    account.sync_error_code = error_code
    account.sync_status_at = utcnow()
    session.add(account)
    session.commit()


def _collect_messages(access_token: str) -> list[dict]:
    url: str | None = _MESSAGES_URL
    pages = 0
    items: list[dict] = []
    while url and pages < 4:
        if not url.startswith(_GRAPH_HOST):
            raise ValueError("unexpected message page host")
        payload = _graph_get(url, access_token)
        values = payload.get("value", [])
        if isinstance(values, list):
            items.extend(item for item in values if isinstance(item, dict))
        pages += 1
        link = payload.get("@odata.nextLink")
        url = link if isinstance(link, str) else None
    return items


async def sync_mailbox_delta(
    ctx: dict,
    subscription_id: str,
    generation: int | None = None,
) -> None:
    """Load one mailbox, refresh its token when needed, and store its messages.

    `generation` is the sync_generation captured when the job was queued. A
    disconnect or a newer sync bumps that counter and this job returns without
    writing. Webhook jobs omit it and still refuse a disconnected mailbox.
    Success is the only path that sets last_sync_at. Failures return normally
    so a retry cannot mark an already-failed or superseded generation idle.
    """
    claimed: int | None = _generation(generation) if generation is not None else None
    account_id: UUID | None = None
    with Session(engine) as session:
        account = _load_account(session, subscription_id)
        if account is None:
            logger.warning("No connected account for sync job_id=%s", ctx.get("job_id"))
            return
        account_id = account.id
        if claimed is None:
            claimed = int(account.sync_generation or 0)
        locked = _lock_account(session, account_id)
        if locked is None or _stale(locked, claimed):
            session.rollback()
            return
        locked.sync_status = "running"
        locked.sync_error_code = None
        locked.sync_status_at = utcnow()
        session.add(locked)
        session.commit()
        try:
            if needs_refresh(locked):
                locked = refresh_access_token(session, locked, generation=claimed)
            if locked.access_token is None:
                raise MailboxNotWritable()
            items = _collect_messages(locked.access_token)
        except MailboxNotWritable:
            logger.info("sync_mailbox_delta stopped account=%s job_id=%s", account_id, ctx.get("job_id"))
            return
        except TokenRefreshError as exc:
            logger.warning(
                "sync_mailbox_delta failed account=%s code=%s error_type=%s",
                account_id,
                TOKEN_REFRESH_FAILED,
                type(exc).__name__,
            )
            _mark_failed(session, account_id, claimed, TOKEN_REFRESH_FAILED)
            return
        except Exception as exc:
            logger.warning(
                "sync_mailbox_delta failed account=%s code=%s error_type=%s",
                account_id,
                GRAPH_UNAVAILABLE,
                type(exc).__name__,
            )
            _mark_failed(session, account_id, claimed, GRAPH_UNAVAILABLE)
            return

        try:
            locked = _lock_account(session, account_id)
            if locked is None or _stale(locked, claimed):
                session.rollback()
                logger.info(
                    "sync_mailbox_delta discarded account=%s job_id=%s",
                    account_id,
                    ctx.get("job_id"),
                )
                return
            for item in items:
                _upsert_message(session, locked, item)
            locked.sync_status = "idle"
            locked.sync_error_code = None
            locked.last_sync_at = utcnow()
            locked.sync_status_at = locked.last_sync_at
            session.add(locked)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning(
                "sync_mailbox_delta failed account=%s code=%s error_type=%s",
                account_id,
                GRAPH_UNAVAILABLE,
                type(exc).__name__,
            )
            _mark_failed(session, account_id, claimed, GRAPH_UNAVAILABLE)
            return
    logger.info("sync_mailbox_delta finished account=%s job_id=%s", account_id, ctx.get("job_id"))


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
