"""Save one Slack workspace for the user who started OAuth."""

from uuid import UUID

from sqlmodel import Session

from app.services.graph.connect import ConnectOutcome, connect_mailbox


def connect_slack_mailbox(
    session: Session,
    *,
    user_id: UUID,
    provider_account_id: str,
    team_id: str,
    email: str,
    access_token: str,
    scopes: str | None,
) -> ConnectOutcome:
    """Save a Slack user token. There is no refresh token when rotation is off."""
    return connect_mailbox(
        session,
        provider="slack",
        user_id=user_id,
        provider_account_id=provider_account_id,
        tenant_id=team_id,
        email=email,
        access_token=access_token,
        refresh_token=None,
        expires_at=None,
        scopes=scopes,
    )
