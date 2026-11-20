"""Save one Google mailbox for the user who started OAuth.

The code is exchanged and userinfo is read before this runs. Tokens are
encrypted by the connected_accounts column type.
"""

from datetime import datetime
from uuid import UUID

from sqlmodel import Session

from app.services.graph.connect import ConnectOutcome, connect_mailbox


def connect_google_mailbox(
    session: Session,
    *,
    user_id: UUID,
    provider_account_id: str,
    email: str,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    scopes: str | None,
    expected_mailbox_id: UUID | None = None,
) -> ConnectOutcome:
    """Save a Google mailbox. `provider_account_id` is the OpenID `sub`."""
    return connect_mailbox(
        session,
        provider="google",
        user_id=user_id,
        provider_account_id=provider_account_id,
        tenant_id=None,
        email=email,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
        expected_mailbox_id=expected_mailbox_id,
    )
