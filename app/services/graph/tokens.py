"""Read and write Graph OAuth tokens on a connected account.

Tokens are still stored in plaintext. This file must not log them.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlmodel import Session

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow

_REFRESH_WITHIN = timedelta(minutes=5)


def get_account(session: Session, account_id: UUID) -> ConnectedAccount:
    """Load one ConnectedAccount or raise if it is missing."""
    account = session.get(ConnectedAccount, account_id)
    if account is None:
        raise LookupError(f"Connected account {account_id} was not found")
    return account


def save_tokens(
    session: Session,
    account: ConnectedAccount,
    *,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: str | None,
) -> None:
    """Set the token columns, stamp updated_at, and commit."""
    account.access_token = access_token
    account.refresh_token = refresh_token
    account.token_expires_at = expires_at
    account.scopes = scopes
    account.updated_at = utcnow()
    session.add(account)
    session.commit()


def needs_refresh(account: ConnectedAccount) -> bool:
    """True when token_expires_at is missing or less than five minutes away."""
    expires_at = account.token_expires_at
    if expires_at is None:
        return True
    return expires_at <= utcnow() + _REFRESH_WITHIN
