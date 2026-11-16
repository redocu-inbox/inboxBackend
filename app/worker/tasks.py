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

from app.config import get_settings
from app.db import engine
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.user import utcnow
from app.models.connected_account import ConnectedAccount
from app.services.llm.factory import get_llm
from app.services.llm.stub import StubLLM
from app.services.reply_drafts.generation import build_reply_prompts, final_reply_body
from app.services.google.messages import (
    GmailRequestError,
    collect_gmail_messages,
    gmail_message_fields,
)
from app.services.google.tokens import GoogleTokenUnavailable, refresh_google_access_token
from app.services.graph.html_text import to_plain_text
from app.services.graph.tokens import (
    MailboxNotWritable,
    TokenRefreshError,
    needs_refresh,
    refresh_access_token,
)
from app.services.mailboxes import (
    GMAIL_UNAVAILABLE,
    GRAPH_UNAVAILABLE,
    TOKEN_REFRESH_FAILED,
    sync_in_flight,
)
from app.services.forms.fill_job import execute_form_fill
from app.services.forms.match import consider_form_match_on_sync, execute_form_match
from app.services.priority.apply import classify_and_score, locked_fields
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


def _upsert_message(
    session: Session,
    account: ConnectedAccount,
    item: dict,
    mapper=graph_message_fields,
) -> Message | None:
    """Insert a message or update it when this mailbox already stored that provider id."""
    fields = mapper(item)
    if fields is None:
        return None
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
    return existing


def _classify_saved(session: Session, row: Message | None) -> None:
    """Classify and score one upserted row. Corrected fields stay as stored."""
    if row is None:
        return
    held = locked_fields(session, [row.id]).get(row.id, set())
    classify_and_score(row, locked=held)


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


def _sync_google_mailbox(session: Session, account_id: UUID, claimed: int | None, ctx: dict) -> None:
    """List recent Gmail INBOX messages and store them. Never calls Microsoft Graph.

    A 401 refreshes once. `invalid_grant` and a second 401 set token_refresh_failed.
    A token-endpoint timeout, network error, or HTTP 5xx sets gmail_unavailable
    and does not ask the user to reconnect. Other Gmail errors set
    gmail_unavailable. The job returns either way.
    """
    locked = _lock_account(session, account_id)
    if locked is None or locked.provider != "google" or _stale(locked, claimed):
        session.rollback()
        return
    locked.sync_status = "running"
    locked.sync_error_code = None
    locked.sync_status_at = utcnow()
    session.add(locked)
    session.commit()
    try:
        if needs_refresh(locked):
            locked = refresh_google_access_token(session, locked, generation=claimed)
        if locked.access_token is None:
            raise MailboxNotWritable()
        try:
            items = collect_gmail_messages(locked.access_token)
        except GmailRequestError as exc:
            if exc.status != 401:
                raise
            locked = refresh_google_access_token(session, locked, generation=claimed)
            if locked.access_token is None:
                raise MailboxNotWritable()
            items = collect_gmail_messages(locked.access_token)
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
    except GmailRequestError as exc:
        code = TOKEN_REFRESH_FAILED if exc.status == 401 else GMAIL_UNAVAILABLE
        logger.warning(
            "sync_mailbox_delta failed account=%s code=%s status=%s",
            account_id,
            code,
            exc.status,
        )
        _mark_failed(session, account_id, claimed, code)
        return
    except GoogleTokenUnavailable as exc:
        logger.warning(
            "sync_mailbox_delta failed account=%s code=%s error_type=%s",
            account_id,
            GMAIL_UNAVAILABLE,
            type(exc).__name__,
        )
        _mark_failed(session, account_id, claimed, GMAIL_UNAVAILABLE)
        return
    except Exception as exc:
        logger.warning(
            "sync_mailbox_delta failed account=%s code=%s error_type=%s",
            account_id,
            GMAIL_UNAVAILABLE,
            type(exc).__name__,
        )
        _mark_failed(session, account_id, claimed, GMAIL_UNAVAILABLE)
        return

    try:
        locked = _lock_account(session, account_id)
        if locked is None or locked.provider != "google" or _stale(locked, claimed):
            session.rollback()
            logger.info("sync_mailbox_delta discarded account=%s job_id=%s", account_id, ctx.get("job_id"))
            return
        for item in items:
            _classify_saved(session, _upsert_message(session, locked, item, gmail_message_fields))
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
            GMAIL_UNAVAILABLE,
            type(exc).__name__,
        )
        _mark_failed(session, account_id, claimed, GMAIL_UNAVAILABLE)
        return
    consider_form_match_on_sync(ctx, account_id)
    logger.info("sync_mailbox_delta finished account=%s job_id=%s", account_id, ctx.get("job_id"))


async def sync_mailbox_delta(
    ctx: dict,
    subscription_id: str,
    generation: int | None = None,
) -> None:
    """Load one mailbox, refresh its token when needed, and store its messages.

    `generation` is the sync_generation captured when the job was queued. A
    disconnect or a newer sync bumps that counter and this job returns without
    writing. Webhook jobs omit it and still refuse a disconnected mailbox.
    A Google mailbox is listed from Gmail INBOX and is not sent to Graph.
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
        if account.provider == "google":
            _sync_google_mailbox(session, account_id, claimed, ctx)
            return
        if account.provider != "microsoft":
            logger.info(
                "sync_mailbox_delta skipped account=%s provider=%s",
                account.id,
                account.provider,
            )
            return
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
                _classify_saved(session, _upsert_message(session, locked, item))
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
    consider_form_match_on_sync(ctx, account_id)
    logger.info("sync_mailbox_delta finished account=%s job_id=%s", account_id, ctx.get("job_id"))


async def rescore_mailbox_priority(ctx: dict, account_id: str) -> int | None:
    """Classify and score mail already stored for one mailbox.

    Returns how many stored messages were walked, or None when the id is not a
    UUID or the mailbox is missing or disconnected. Worker startup calls this
    for every connected mailbox unless RESCORE_ON_STARTUP=0. It is not on the
    cron, and sync, connect, and the webhook do not enqueue it. A corrected
    field stays as the user saved it. This job does not call Graph or Gmail
    and does not change sync status.
    """
    del ctx
    try:
        account_uuid = UUID(account_id)
    except ValueError:
        return None
    with Session(engine) as session:
        account = session.get(ConnectedAccount, account_uuid)
        if account is None or account.disconnected_at is not None:
            return None
        rows = session.exec(
            select(Message).where(Message.connected_account_id == account.id)
        ).all()
        held = locked_fields(session, [row.id for row in rows])
        for row in rows:
            if classify_and_score(row, locked=held.get(row.id, set())):
                row.updated_at = utcnow()
                session.add(row)
        session.commit()
        return len(rows)


def _connected_mailbox_ids() -> list[UUID]:
    """Ids of mailboxes that are still connected. Tokens are not loaded."""
    with Session(engine) as session:
        rows = session.exec(
            select(ConnectedAccount.id)
            .where(ConnectedAccount.disconnected_at.is_(None))
            .order_by(ConnectedAccount.id)
        ).all()
    return list(rows)


async def rescore_connected_mailboxes(ctx: dict | None = None) -> tuple[int, int]:
    """Rescore every connected mailbox. Log and continue when one of them fails.

    Called from worker startup. The work is the same `rescore_mailbox_priority`
    job, run here so one log line can report accounts and stored messages.
    Enqueueing would return before those jobs finished, and this process is
    not polling yet. A disconnected mailbox is omitted. This does not call
    Graph, Gmail, or an LLM, and it does not write mailbox sync columns.
    """
    if not get_settings().rescore_on_startup:
        logger.info("priority rescore on startup skipped")
        return 0, 0
    try:
        account_ids = _connected_mailbox_ids()
    except Exception as exc:
        logger.warning(
            "priority rescore on startup failed error_type=%s",
            type(exc).__name__,
        )
        return 0, 0
    accounts = 0
    messages = 0
    for account_id in account_ids:
        try:
            counted = await rescore_mailbox_priority(ctx or {}, str(account_id))
        except Exception as exc:
            logger.warning(
                "priority rescore on startup failed account=%s error_type=%s",
                account_id,
                type(exc).__name__,
            )
            continue
        if counted is None:
            continue
        accounts += 1
        messages += counted
    logger.info(
        "priority rescore on startup finished accounts=%s messages=%s",
        accounts,
        messages,
    )
    return accounts, messages


async def match_form_template(ctx: dict, job_id: str) -> None:
    """Choose a template, extract values, and enqueue a fill when the model returns some.

    A missing id is logged and ignored. The stub stops at needs_review and
    does not enqueue run_form_fill. This job does not call Graph or Gmail.
    """
    await execute_form_match(ctx, job_id)


async def run_form_fill(ctx: dict, job_id: str) -> None:
    """Write the job's saved values into the AcroForm and store the PDF.

    A missing id is logged and ignored. Running the job again overwrites the
    same storage key. Confidence is left as it was stored at create time.
    """
    del ctx
    execute_form_fill(job_id)


async def renew_graph_subscriptions(ctx: dict) -> None:
    """TODO: renew subscriptions that expire within a day."""
    logger.info("TODO renew_graph_subscriptions job_id=%s", ctx.get("job_id"))


def _claim_periodic_syncs() -> tuple[list[tuple[UUID, int]], int]:
    """Mark connected mailboxes pending unless a sync is already in flight.

    Returns the (account id, generation) pairs to sync, and how many mailboxes
    were left alone. A pending or running sync inside the stale window is left
    alone so this cron does not start a second run. Failed and idle mailboxes
    are claimed. Disconnected rows are not loaded.
    """
    due: list[tuple[UUID, int]] = []
    skipped = 0
    with Session(engine) as session:
        accounts = list(
            session.exec(
                select(ConnectedAccount)
                .where(ConnectedAccount.disconnected_at.is_(None))
                .where(ConnectedAccount.provider.in_(("microsoft", "google")))
                .order_by(ConnectedAccount.id)
                .with_for_update()
            ).all()
        )
        now = utcnow()
        changed = False
        for account in accounts:
            if not account.access_token or not account.refresh_token:
                skipped += 1
                continue
            if sync_in_flight(account, now=now):
                skipped += 1
                continue
            account.sync_generation = int(account.sync_generation or 0) + 1
            account.sync_status = "pending"
            account.sync_error_code = None
            account.sync_status_at = now
            account.updated_at = now
            session.add(account)
            changed = True
            due.append((account.id, int(account.sync_generation)))
        if changed:
            session.commit()
        else:
            session.rollback()
    return due, skipped


async def sync_connected_mailboxes(ctx: dict) -> dict[str, int]:
    """Sync every connected Outlook and Gmail mailbox.

    Calls the same `sync_mailbox_delta` path as a manual sync, including its
    row lock and generation check. A mailbox that is already pending or running
    is skipped. A token refresh failure or a provider error, including HTTP 403,
    stays on that account and the loop continues. The free Render service sleeps
    after inactivity, so this cron runs only while that process is awake.
    """
    if not get_settings().periodic_mailbox_sync:
        logger.info("periodic mailbox sync skipped")
        return {"ran": 0, "skipped": 0, "errors": 0}
    try:
        due, skipped = _claim_periodic_syncs()
    except Exception as exc:
        logger.warning(
            "periodic mailbox sync failed error_type=%s",
            type(exc).__name__,
        )
        return {"ran": 0, "skipped": 0, "errors": 1}
    ran = 0
    errors = 0
    for account_id, generation in due:
        try:
            await sync_mailbox_delta(ctx, str(account_id), generation)
        except Exception as exc:
            errors += 1
            logger.warning(
                "periodic mailbox sync failed account=%s error_type=%s",
                account_id,
                type(exc).__name__,
            )
            continue
        ran += 1
    logger.info(
        "periodic mailbox sync finished ran=%s skipped=%s errors=%s",
        ran,
        skipped,
        errors,
    )
    return {"ran": ran, "skipped": skipped, "errors": errors}


def _reply_status(value: object) -> str:
    if isinstance(value, ReplyDraftStatus):
        return value.value
    return str(value)


def _pending_reply_fields(message_id: str) -> tuple[str | None, str | None, str | None] | None:
    """Copy the prompt strings while the session is open. None when nothing is pending."""
    try:
        parsed = UUID(message_id)
    except ValueError:
        logger.warning("generate_reply_draft skipped invalid id")
        return None
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == parsed)).first()
        if draft is None or _reply_status(draft.status) != ReplyDraftStatus.pending.value:
            return None
        message = session.get(Message, parsed)
        if message is None:
            return None
        return message.subject, message.body_text, message.body_preview


def _fail_reply_draft(message_id: str) -> None:
    """Mark a still-pending draft failed. The stored body is left as it is."""
    try:
        parsed = UUID(message_id)
    except ValueError:
        return
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == parsed)).first()
        if draft is None or _reply_status(draft.status) != ReplyDraftStatus.pending.value:
            return
        draft.status = ReplyDraftStatus.failed
        draft.error = "generation failed"
        draft.updated_at = utcnow()
        session.add(draft)
        session.commit()


def _save_reply_draft(message_id: str, body: str) -> None:
    """Store a generated reply only while the row is still pending."""
    try:
        parsed = UUID(message_id)
    except ValueError:
        return
    with Session(engine) as session:
        draft = session.exec(select(ReplyDraft).where(ReplyDraft.message_id == parsed)).first()
        if draft is None or _reply_status(draft.status) != ReplyDraftStatus.pending.value:
            return
        draft.body = body
        draft.status = ReplyDraftStatus.ready
        draft.source = "llm"
        draft.error = None
        draft.updated_at = utcnow()
        session.add(draft)
        session.commit()


async def generate_reply_draft(ctx: dict, message_id: str) -> None:
    """Write a local reply body. Does not call Graph and does not send mail.

    Prompt fields are copied inside the session. `get_llm` runs after that
    session closes. A failure sets `error` to `generation failed` and keeps
    the body already stored, including a regenerate that POST left pending.
    """
    fields = _pending_reply_fields(message_id)
    if fields is None:
        return
    subject, body_text, body_preview = fields
    try:
        llm = get_llm()
        system, prompt = build_reply_prompts(
            subject=subject,
            body_text=body_text,
            body_preview=body_preview,
        )
        model_text = await llm.complete(system=system, user=prompt)
        provider = "stub" if isinstance(llm, StubLLM) else get_settings().llm_provider
        reply = final_reply_body(provider, model_text)
    except Exception as exc:
        logger.warning(
            "generate_reply_draft failed message=%s error_type=%s job_id=%s",
            message_id,
            type(exc).__name__,
            ctx.get("job_id"),
        )
        _fail_reply_draft(message_id)
        return
    _save_reply_draft(message_id, reply)
