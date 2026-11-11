"""Refresh a Google mailbox access token.

The token columns decrypt on read. This file handles plaintext and must not log it.
Google often omits a new refresh token. The stored one is kept in that case.
"""

from datetime import timedelta

from sqlmodel import Session

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow
from app.services.google.oauth import GoogleTokenUnavailable, exchange_refresh_token
from app.services.graph.tokens import MailboxNotWritable, TokenRefreshError, save_tokens


def refresh_google_access_token(
    session: Session,
    account: ConnectedAccount,
    *,
    generation: int | None = None,
) -> ConnectedAccount:
    """Exchange the stored refresh token.

    A missing refresh token or Google's `invalid_grant` raises TokenRefreshError.
    Any other token-endpoint failure raises GoogleTokenUnavailable and leaves
    the stored grant in place.
    """
    if not account.refresh_token:
        raise TokenRefreshError("no_refresh_token")
    result = exchange_refresh_token(account.refresh_token)
    if "access_token" not in result:
        raise GoogleTokenUnavailable()
    refresh_token = result.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = account.refresh_token
    try:
        expires_in = int(result["expires_in"])
    except (KeyError, TypeError, ValueError):
        raise GoogleTokenUnavailable() from None
    scope = result.get("scope")
    saved = save_tokens(
        session,
        account,
        access_token=result["access_token"],
        refresh_token=refresh_token,
        expires_at=utcnow() + timedelta(seconds=expires_in),
        scopes=scope if isinstance(scope, str) else account.scopes,
        generation=generation,
    )
    if saved is None:
        raise MailboxNotWritable()
    return saved
