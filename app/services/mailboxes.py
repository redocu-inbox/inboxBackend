"""One active Outlook mailbox per app user, and the sync lifecycle for it.

Historical rows are kept. Disconnect and the one-mailbox migration set
`disconnected_at` and, on an explicit disconnect, clear tokens. They do not
delete messages or another user's rows.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow

# A crashed worker must not pin the mailbox in pending/running forever.
SYNC_STALE_AFTER = timedelta(minutes=10)

ACTIVE_MAILBOX_INDEX = "uq_connected_accounts_one_active_microsoft"

# Keeps the newest active Microsoft row per user. Does not touch tokens,
# delta links, or messages. Other active rows for that user are only marked
# disconnected so the partial unique index can be created.
DEACTIVATE_EXTRA_MICROSOFT_MAILBOXES = """
UPDATE connected_accounts AS extra
SET disconnected_at = NOW()
WHERE extra.disconnected_at IS NULL
  AND extra.provider = 'microsoft'
  AND extra.id NOT IN (
      SELECT id FROM (
          SELECT DISTINCT ON (user_id) id
          FROM connected_accounts
          WHERE disconnected_at IS NULL
            AND provider = 'microsoft'
          ORDER BY user_id, updated_at DESC, id DESC
      ) AS keeper
  )
"""

SYNC_STATUSES = ("idle", "pending", "running", "failed")
QUEUE_UNAVAILABLE = "queue_unavailable"
TOKEN_REFRESH_FAILED = "token_refresh_failed"
GRAPH_UNAVAILABLE = "graph_unavailable"


def deactivate_extra_microsoft_mailboxes(connection) -> None:
    """Hide extra active mailboxes without deleting their tokens or mail."""
    connection.execute(text(DEACTIVATE_EXTRA_MICROSOFT_MAILBOXES))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


def sync_in_flight(account: ConnectedAccount, *, now: datetime | None = None) -> bool:
    """True when a live job should be left alone. Stale pending/running is not in flight."""
    if account.sync_status not in {"pending", "running"}:
        return False
    stamp = _aware(account.sync_status_at)
    if stamp is None:
        return True
    current = now or utcnow()
    return current - stamp < SYNC_STALE_AFTER


@dataclass(frozen=True)
class SyncEnqueue:
    enqueued: bool
    sync_status: str
    generation: int
    account_id: UUID


def lock_active_mailbox(session: Session, user_id: UUID) -> ConnectedAccount | None:
    """Lock this user's active Microsoft mailbox, if they have one."""
    return session.exec(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.disconnected_at.is_(None),
        )
        .order_by(ConnectedAccount.updated_at.desc(), ConnectedAccount.id.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def request_mailbox_sync(session: Session, user_id: UUID) -> SyncEnqueue | None:
    """Mark the active mailbox pending and bump its generation, unless a job is in flight.

    Returns None when no active mailbox exists. `enqueued` is false when this call
    did not take a new generation (already pending or running, and not stale).
    """
    account = lock_active_mailbox(session, user_id)
    if account is None:
        return None
    if sync_in_flight(account):
        result = SyncEnqueue(
            enqueued=False,
            sync_status=account.sync_status,
            generation=int(account.sync_generation or 0),
            account_id=account.id,
        )
        session.rollback()
        return result
    now = utcnow()
    account.sync_generation = int(account.sync_generation or 0) + 1
    account.sync_status = "pending"
    account.sync_error_code = None
    account.sync_status_at = now
    account.updated_at = now
    session.add(account)
    session.commit()
    session.refresh(account)
    return SyncEnqueue(
        enqueued=True,
        sync_status="pending",
        generation=int(account.sync_generation),
        account_id=account.id,
    )


def mark_sync_enqueue_failed(session: Session, account_id: UUID, generation: int) -> None:
    """Record that this generation never reached the worker. Leave last_sync_at alone."""
    account = session.exec(
        select(ConnectedAccount)
        .where(ConnectedAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if account is None or account.disconnected_at is not None:
        session.rollback()
        return
    if account.sync_generation != generation or account.sync_status != "pending":
        session.rollback()
        return
    account.sync_status = "failed"
    account.sync_error_code = QUEUE_UNAVAILABLE
    account.sync_status_at = utcnow()
    session.add(account)
    session.commit()


def disconnect_active_mailbox(session: Session, user_id: UUID) -> bool:
    """Drop tokens for the active mailbox and keep its messages.

    Bumps sync_generation so a queued or running job cannot write tokens or mail
    back. Other users, form rows, and this user's messages are not deleted.
    """
    account = lock_active_mailbox(session, user_id)
    if account is None:
        return False
    now = utcnow()
    account.disconnected_at = now
    account.access_token = None
    account.refresh_token = None
    account.token_expires_at = None
    account.scopes = None
    account.delta_link = None
    account.sync_generation = int(account.sync_generation or 0) + 1
    account.sync_status = "idle"
    account.sync_error_code = None
    account.sync_status_at = now
    account.updated_at = now
    session.add(account)
    session.commit()
    return True
