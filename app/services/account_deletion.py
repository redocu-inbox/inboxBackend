"""Fence account activity and remove data for accepted deletion requests."""

import logging
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import Connection, delete, text
from sqlmodel import Session, select

from app import storage
from app.config import get_settings
from app.db import engine
from app.models.account_deletion import AccountDeletion
from app.models.user import User, utcnow
from app.services.stripe_cleanup import StripeCleanupError, ensure_stripe_customer_deleted

logger = logging.getLogger(__name__)


def _lock_key(user_id: UUID) -> int:
    digest = blake2b(user_id.bytes, digest_size=8, person=b"account-deletion").digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def account_is_deleting(session: Session, user_id: UUID) -> bool:
    return session.scalar(select(AccountDeletion.user_id).where(AccountDeletion.user_id == user_id)) is not None


def try_lock_deletion(session: Session, user_id: UUID) -> bool:
    # An API request may hold a shared lock across a provider call. Let deletion
    # callers retry instead of queuing behind that lock and blocking new workers.
    return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _lock_key(user_id)}))


def lock_account_write(session: Session, user_id: UUID) -> bool:
    """Guard a worker write from this check through commit or rollback."""
    with session.no_autoflush:
        session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": _lock_key(user_id)})
        return not account_is_deleting(session, user_id)


def lock_account_request(session: Session, user_id: UUID) -> None:
    """Guard an authenticated request, including routes that commit more than once."""
    key = _lock_key(user_id)
    session.execute(text("SELECT pg_advisory_lock_shared(:key)"), {"key": key})
    session.info["account_request_lock"] = key


def unlock_account_request(connection: Connection, key: int) -> None:
    try:
        connection.execute(text("SELECT pg_advisory_unlock_shared(:key)"), {"key": key})
    except Exception:
        # Never return a connection with a session-level lock to the pool.
        connection.invalidate()
        raise


def purge_pending_accounts() -> None:
    """Drain the Postgres queue, leaving failures eligible for the next run."""
    with Session(engine) as session:
        user_ids = session.exec(
            select(AccountDeletion.user_id)
            .where(AccountDeletion.local_purged_at.is_(None))
            .order_by(AccountDeletion.requested_at, AccountDeletion.user_id)
        ).all()
    for user_id in user_ids:
        try:
            _purge_account(user_id)
        except Exception as exc:
            logger.warning("Account data purge failed error_type=%s", type(exc).__name__)


def _purge_account(user_id: UUID) -> None:
    with Session(engine) as session:
        request = session.exec(
            select(AccountDeletion)
            .where(AccountDeletion.user_id == user_id, AccountDeletion.local_purged_at.is_(None))
            .with_for_update(skip_locked=True)
        ).first()
        if request is None:
            return

        if request.stripe_customer_id is None and request.stripe_deleted_at is None:
            # Older pending requests can pick up a trusted billing backfill.
            request.stripe_customer_id = session.scalar(
                select(User.stripe_customer_id).where(User.id == user_id)
            )
        # The committed request already fences writers. Remove the owned trees
        # before cascading the database rows, including files left by lost rows.
        file_store = storage.get_storage()
        file_store.delete_tree(f"forms/{user_id}")
        file_store.delete_tree(f"attachments/{user_id}")
        session.execute(delete(User).where(User.id == user_id))
        request.local_purged_at = utcnow()
        session.commit()


def purge_pending_stripe_customers() -> None:
    """Retry unfinished billing cleanup independently of local file and row removal."""
    api_key = get_settings().stripe_secret_key.get_secret_value().strip()
    if not api_key:
        return

    with Session(engine) as session:
        user_ids = session.exec(
            select(AccountDeletion.user_id)
            .where(AccountDeletion.stripe_customer_id.is_not(None), AccountDeletion.stripe_deleted_at.is_(None))
            .order_by(AccountDeletion.requested_at, AccountDeletion.user_id)
        ).all()
    for user_id in user_ids:
        try:
            _purge_stripe_customer(api_key, user_id)
        except StripeCleanupError as exc:
            logger.warning("Account Stripe cleanup pending reason=%s", exc)
        except Exception as exc:
            logger.warning("Account Stripe cleanup failed error_type=%s", type(exc).__name__)


def _purge_stripe_customer(api_key: str, user_id: UUID) -> None:
    with Session(engine) as session:
        deletion = session.exec(
            select(AccountDeletion)
            .where(AccountDeletion.user_id == user_id, AccountDeletion.stripe_deleted_at.is_(None))
            .with_for_update(skip_locked=True)
        ).first()
        if deletion is None or deletion.stripe_customer_id is None:
            return

        ensure_stripe_customer_deleted(api_key, deletion.stripe_customer_id, user_id)
        deletion.stripe_customer_id = None
        deletion.stripe_deleted_at = utcnow()
        session.commit()
