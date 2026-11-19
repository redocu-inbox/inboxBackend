"""Read and write Graph OAuth tokens on a connected account.

The token columns encrypt on write and decrypt on read, so this file handles
plaintext. It must not log them.
"""
import logging

from datetime import datetime, timedelta
from uuid import UUID

from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow
from app.services.account_deletion import lock_account_write
from app.services.graph.oauth import build_msal_app
from app.config import get_settings

_REFRESH_WITHIN = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def get_account(session: Session, account_id: UUID) -> ConnectedAccount:
    """Load one ConnectedAccount or raise if it is missing."""
    account = session.get(ConnectedAccount, account_id)
    if account is None:
        raise LookupError(f"Connected account {account_id} was not found")
    return account


class MailboxNotWritable(Exception):
    """The mailbox was disconnected or a newer sync generation owns it."""


class TokenRefreshError(RuntimeError):
    """The refresh token could not be exchanged. The message is a stable code, not a secret."""


def save_tokens(
    session: Session,
    account: ConnectedAccount,
    *,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: str | None,
    generation: int | None = None,
) -> ConnectedAccount | None:
    """Set the token columns and commit, unless the row was disconnected or superseded.

    Returns the locked row after commit, or None when nothing was written.
    A None result rolls back this unit of work and does not restore cleared tokens.
    """
    if not lock_account_write(session, account.user_id):
        session.rollback()
        return None
    locked = session.exec(
        select(ConnectedAccount)
        .where(ConnectedAccount.id == account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if locked is None or locked.disconnected_at is not None:
        session.rollback()
        return None
    if generation is not None and int(locked.sync_generation or 0) != generation:
        session.rollback()
        return None
    locked.access_token = access_token
    locked.refresh_token = refresh_token
    locked.token_expires_at = expires_at
    locked.scopes = scopes
    locked.updated_at = utcnow()
    session.add(locked)
    session.commit()
    session.refresh(locked)
    return locked

def refresh_access_token(
    session: Session,
    account: ConnectedAccount,
    *,
    generation: int | None = None,
) -> ConnectedAccount:
    """Exchange the stored refresh token for a new access token and save both."""
    if not account.refresh_token:
        raise TokenRefreshError("no_refresh_token")
    result = build_msal_app().acquire_token_by_refresh_token(
        account.refresh_token,
        scopes=get_settings().graph_scope_list,
    )
    if "error" in result or "access_token" not in result:
        logger.warning("Microsoft token refresh failed: %s", result.get("error"))
        raise TokenRefreshError("refresh_rejected")
    # Microsoft may return a new refresh token and retire the old one.
    refresh_token = result.get("refresh_token") or account.refresh_token
    try:
        expires_in = int(result["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise TokenRefreshError("refresh_rejected") from None
    expires_at = utcnow() + timedelta(seconds=expires_in)
    saved = save_tokens(
        session,
        account,
        access_token=result["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=result.get("scope") or account.scopes,
        generation=generation,
    )
    if saved is None:
        raise MailboxNotWritable()
    return saved


def needs_refresh(account: ConnectedAccount) -> bool:
    """True when token_expires_at is missing or less than five minutes away."""
    expires_at = account.token_expires_at
    if expires_at is None:
        return True
    return expires_at <= utcnow() + _REFRESH_WITHIN
