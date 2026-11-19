"""Mailbox jobs.

ARQ job names are these function names. The first argument, `ctx`, is the dict
ARQ passes in. Further arguments are strings. Form jobs live in form_tasks.
"""
import json
import logging
import time
from uuid import UUID
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models.reply_draft import ReplyDraft, ReplyDraftStatus
from app.models.user import utcnow
from app.models.connected_account import ConnectedAccount
from app.services.account_deletion import account_is_deleting, lock_account_write
from app.services.llm.factory import get_llm
from app.services.llm.stub import StubLLM
from app.services.reply_drafts.generation import build_reply_prompts, final_reply_body
from app.services.google.messages import (
    GmailRequestError,
    collect_gmail_messages,
    gmail_message_fields,
)
from app.services.google.tokens import GoogleTokenUnavailable, refresh_google_access_token
from app.services.graph.client import GraphClient
from app.services.graph.html_text import to_plain_text
from app.services.inbox.body_html import sanitize_body_html
from app.services.inbox.recipients import graph_people
from app.services.graph.subscriptions import (
    GraphSubscriptionError,
    create_graph_subscription,
    renew_graph_subscription,
    retry_after_seconds,
)
from app.services.graph.tokens import (
    MailboxNotWritable,
    TokenRefreshError,
    needs_refresh,
    refresh_access_token,
)
from app.services.mailboxes import (
    GMAIL_UNAVAILABLE,
    GRAPH_UNAVAILABLE,
    SLACK_RATE_LIMITED,
    SLACK_UNAVAILABLE,
    TOKEN_REFRESH_FAILED,
    sync_in_flight,
)
from app.services.slack.http import SlackRateLimited
from app.services.slack.sync import collect_slack_messages
from app.services.forms.match import consider_form_match_on_sync
from app.services.priority.apply import classify_and_score, locked_fields
from app.models.message import Message

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
    content_type = body.get("contentType")
    conversation = item.get("conversationId")
    fields = {
        "graph_id": graph_id[:512],
        "subject": to_plain_text(item.get("subject")),
        "body_preview": to_plain_text(item.get("bodyPreview")),
        "received_at": received_at,
        "from_name": to_plain_text(email_address.get("name")),
        "from_email": _plain_address(email_address.get("address")),
        "is_read": is_read if isinstance(is_read, bool) else None,
        "body_text": to_plain_text(content),
        "conversation_id": conversation[:512] if isinstance(conversation, str) and conversation else None,
        "to_recipients": graph_people(item.get("toRecipients")),
        "cc_recipients": graph_people(item.get("ccRecipients")),
        "reply_to": graph_people(item.get("replyTo")),
        "direction": "incoming",
        "origin": "provider",
    }
    if content is not None and isinstance(content_type, str):
        fields["body_html"] = sanitize_body_html(content) if content_type.lower() == "html" else None
    return fields


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
        if existing.inbox_removed_at is not None:
            return None
        for key, value in fields.items():
            if key == "graph_id":
                continue
            setattr(existing, key, value)
        existing.updated_at = utcnow()
    session.add(existing)
    return existing


def _classify_saved(session: Session, row: Message | None, *, provider: str | None = None) -> None:
    """Classify and score one upserted row. Corrected fields stay as stored."""
    if row is None:
        return
    held = locked_fields(session, [row.id]).get(row.id, set())
    classify_and_score(row, locked=held, provider=provider)


def _has_sync_credentials(account: ConnectedAccount) -> bool:
    """Slack user tokens are long-lived and have no refresh token."""
    if not account.access_token:
        return False
    if account.provider == "slack":
        return True
    return bool(account.refresh_token)


logger = logging.getLogger(__name__)


def _generation(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _lock_account(session: Session, account_id: UUID) -> ConnectedAccount | None:
    with session.no_autoflush:
        user_id = session.scalar(select(ConnectedAccount.user_id).where(ConnectedAccount.id == account_id))
        if user_id is None or not lock_account_write(session, user_id):
            return None
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
    if not _has_sync_credentials(account):
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


def _delete_provider_message(session: Session, account: ConnectedAccount, graph_id: str) -> None:
    """Delete a removed Graph row unless an app action already hid it."""
    row = session.exec(
        select(Message).where(
            Message.connected_account_id == account.id,
            Message.graph_id == graph_id[:512],
        )
    ).first()
    if row is not None and row.inbox_removed_at is None:
        session.delete(row)


def _subscription_settings(account_id: UUID) -> tuple[str, str] | None:
    """Webhook URL and client state, or None when a subscription must not be created."""
    settings = get_settings()
    url = settings.graph_webhook_url.strip()
    state = settings.graph_webhook_client_state
    if settings.app_env != "development" and not state:
        logger.error(
            "graph subscription refused account=%s reason=empty_client_state",
            account_id,
        )
        return None
    if not url or not state:
        logger.info(
            "graph subscription skipped account=%s reason=webhook_not_configured",
            account_id,
        )
        return None
    return url, state


def _store_new_subscription(
    session: Session,
    account: ConnectedAccount,
    *,
    fail_on_rate_limit: bool,
) -> None:
    """POST a subscription and store its id. Sync callers do not fail the job on 429."""
    configured = _subscription_settings(account.id)
    if configured is None or not account.access_token:
        return
    url, state = configured
    try:
        subscription_id, expires = create_graph_subscription(
            account.access_token,
            notification_url=url,
            client_state=state,
        )
    except GraphSubscriptionError as exc:
        if exc.status == 429 and fail_on_rate_limit:
            time.sleep(retry_after_seconds(exc.retry_after))
            raise
        logger.warning(
            "graph subscription create failed account=%s status=%s",
            account.id,
            exc.status,
        )
        return
    current = _lock_account(session, account.id)
    if current is None or current.disconnected_at is not None or current.provider != "microsoft":
        return
    current.graph_subscription_id = subscription_id
    current.graph_subscription_expires_at = expires
    current.updated_at = utcnow()
    session.add(current)
    session.commit()


def _ensure_graph_subscription(session: Session, account_id: UUID) -> None:
    """Create a subscription after a successful Outlook sync when none is stored."""
    account = session.get(ConnectedAccount, account_id)
    if (
        account is None
        or account.provider != "microsoft"
        or account.disconnected_at is not None
        or account.graph_subscription_id
        or account_is_deleting(session, account.user_id)
    ):
        return
    try:
        _store_new_subscription(session, account, fail_on_rate_limit=False)
    except GraphSubscriptionError:
        logger.warning("graph subscription create failed account=%s status=429", account_id)


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
            _classify_saved(
                session,
                _upsert_message(session, locked, item, gmail_message_fields),
                provider="google",
            )
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


def _sync_slack_mailbox(
    session: Session,
    account_id: UUID,
    claimed: int | None,
    ctx: dict,
) -> None:
    """Store DMs, group DMs, and personal mentions. This does not post to Slack."""
    locked = _lock_account(session, account_id)
    if locked is None or locked.provider != "slack" or _stale(locked, claimed):
        session.rollback()
        return
    locked.sync_status = "running"
    locked.sync_error_code = None
    locked.sync_status_at = utcnow()
    session.add(locked)
    session.commit()
    try:
        items = collect_slack_messages(session, locked)
    except SlackRateLimited:
        session.rollback()
        logger.warning(
            "sync_mailbox_delta failed account=%s code=%s",
            account_id,
            SLACK_RATE_LIMITED,
        )
        _mark_failed(session, account_id, claimed, SLACK_RATE_LIMITED)
        return
    except Exception as exc:
        session.rollback()
        logger.warning(
            "sync_mailbox_delta failed account=%s code=%s error_type=%s",
            account_id,
            SLACK_UNAVAILABLE,
            type(exc).__name__,
        )
        _mark_failed(session, account_id, claimed, SLACK_UNAVAILABLE)
        return

    try:
        locked = _lock_account(session, account_id)
        if locked is None or locked.provider != "slack" or _stale(locked, claimed):
            session.rollback()
            logger.info("sync_mailbox_delta discarded account=%s job_id=%s", account_id, ctx.get("job_id"))
            return
        for item in items:
            _classify_saved(
                session,
                _upsert_message(session, locked, item, _mapped_fields),
                provider="slack",
            )
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
            SLACK_UNAVAILABLE,
            type(exc).__name__,
        )
        _mark_failed(session, account_id, claimed, SLACK_UNAVAILABLE)
        return
    consider_form_match_on_sync(ctx, account_id)
    logger.info("sync_mailbox_delta finished account=%s job_id=%s", account_id, ctx.get("job_id"))


def _mapped_fields(item: dict) -> dict | None:
    if not item.get("graph_id") or item.get("received_at") is None:
        return None
    return item


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
        if account.provider == "slack":
            _sync_slack_mailbox(session, account_id, claimed, ctx)
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
            delta = await GraphClient(locked).sync_inbox_delta(_graph_get)
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
        if delta.truncated:
            logger.warning("sync_mailbox_delta truncated account=%s pages=20", account_id)

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
            for removed_id in delta.removed_ids:
                _delete_provider_message(session, locked, removed_id)
            for item in delta.changed:
                _classify_saved(
                    session,
                    _upsert_message(session, locked, item),
                    provider="microsoft",
                )
            if delta.delta_link:
                locked.delta_link = delta.delta_link
            locked.sync_status = "idle"
            locked.sync_error_code = None
            locked.last_sync_at = utcnow()
            locked.sync_status_at = locked.last_sync_at
            session.add(locked)
            session.commit()
            _ensure_graph_subscription(session, locked.id)
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
        if not lock_account_write(session, account.user_id):
            return None
        rows = session.exec(
            select(Message).where(Message.connected_account_id == account.id)
        ).all()
        held = locked_fields(session, [row.id for row in rows])
        for row in rows:
            if classify_and_score(row, locked=held.get(row.id, set()), provider=account.provider):
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


async def renew_graph_subscriptions(ctx: dict) -> None:
    """Renew Outlook subscriptions that expire within 24 hours.

    A 404 creates the subscription again. A 429 waits for Retry-After once
    and then raises so ARQ can retry. Gmail rows are not loaded. Tokens and
    message bodies are not logged.
    """
    soon = utcnow() + timedelta(hours=24)
    with Session(engine) as session:
        rows = session.exec(
            select(ConnectedAccount.id).where(
                ConnectedAccount.provider == "microsoft",
                ConnectedAccount.disconnected_at.is_(None),
                ConnectedAccount.graph_subscription_id.is_not(None),
                ConnectedAccount.graph_subscription_expires_at.is_not(None),
                ConnectedAccount.graph_subscription_expires_at <= soon,
            )
        ).all()
    for account_id in rows:
        _renew_subscription(account_id)
    logger.info(
        "renew_graph_subscriptions finished job_id=%s accounts=%s",
        ctx.get("job_id"),
        len(rows),
    )


def _renew_subscription(account_id: UUID) -> None:
    with Session(engine) as session:
        account = _lock_account(session, account_id)
        if (
            account is None
            or account.provider != "microsoft"
            or account.disconnected_at is not None
            or not account.graph_subscription_id
            or not account.access_token
            or not account.refresh_token
        ):
            session.rollback()
            return
        try:
            if needs_refresh(account):
                account = refresh_access_token(session, account)
        except (TokenRefreshError, MailboxNotWritable) as exc:
            logger.warning(
                "graph subscription refresh failed account=%s error_type=%s",
                account_id,
                type(exc).__name__,
            )
            return
        if (
            not account.access_token
            or not account.graph_subscription_id
            or account_is_deleting(session, account.user_id)
        ):
            return
        try:
            expires = renew_graph_subscription(account.access_token, account.graph_subscription_id)
        except GraphSubscriptionError as exc:
            if exc.status == 404:
                _store_new_subscription(session, account, fail_on_rate_limit=True)
                return
            if exc.status == 429:
                time.sleep(retry_after_seconds(exc.retry_after))
                raise
            logger.warning(
                "graph subscription renew failed account=%s status=%s",
                account_id,
                exc.status,
            )
            return
        current = _lock_account(session, account.id)
        if current is None or current.disconnected_at is not None:
            return
        current.graph_subscription_expires_at = expires
        current.updated_at = utcnow()
        session.add(current)
        session.commit()


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
                .where(ConnectedAccount.provider.in_(("microsoft", "google", "slack")))
                .order_by(ConnectedAccount.id)
                .with_for_update()
            ).all()
        )
        now = utcnow()
        changed = False
        for account in accounts:
            if not _has_sync_credentials(account):
                skipped += 1
                continue
            if sync_in_flight(account, now=now):
                skipped += 1
                continue
            if not lock_account_write(session, account.user_id):
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
    """Sync every connected Outlook, Gmail, and Slack mailbox.

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
        if account_is_deleting(session, draft.user_id):
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
        if not lock_account_write(session, draft.user_id):
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
        if not lock_account_write(session, draft.user_id):
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
