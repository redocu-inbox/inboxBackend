"""Read and write Graph OAuth tokens on a connected account.

The token columns encrypt on write and decrypt on read, so this file handles
plaintext. It must not log them.
"""
import logging

from datetime import datetime, timedelta
from uuid import UUID

from sqlmodel import Session

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow
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

def refresh_access_token(session: Session, account: ConnectedAccount) -> ConnectedAccount:
    """Exchange the stored refresh token for a new access token and save both."""
    if not account.refresh_token:
        raise RuntimeError("No refresh token available")
    result = build_msal_app().acquire_token_by_refresh_token(
        account.refresh_token,
        scopes=get_settings().graph_scope_list,
    )
    if "error" in result or "access_token" not in result:
        logger.warning("Microsoft token refresh failed: %s", result.get("error"))
        raise RuntimeError("Microsoft token refresh failed")
    # Microsoft may return a new refresh token and retire the old one.
    refresh_token = result.get("refresh_token") or account.refresh_token
    expires_at = utcnow() + timedelta(seconds=int(result["expires_in"]))
    save_tokens(
        session,
        account,
        access_token=result["access_token"],
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=result.get("scope") or account.scopes,
    )
    return account


def needs_refresh(account: ConnectedAccount) -> bool:
    """True when token_expires_at is missing or less than five minutes away."""
    expires_at = account.token_expires_at
    if expires_at is None:
        return True
    return expires_at <= utcnow() + _REFRESH_WITHIN
