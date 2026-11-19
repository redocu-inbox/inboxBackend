from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_current_user, get_session
from app.models.user import User
from app.services.google.oauth import revoke_google_token
from app.services.mailboxes import disconnect_mailbox, lock_active_mailbox
from app.services.slack.oauth import revoke_slack_token

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


@router.post("/{account_id}/disconnect")
def disconnect_selected_mailbox(
    account_id: UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict[str, bool]:
    account = lock_active_mailbox(session, user.id, account_id=account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mailbox not found",
        )

    provider = account.provider
    token = account.access_token
    if provider == "google":
        token = account.refresh_token or token

    disconnect_mailbox(session, account)

    if token and provider == "google":
        revoke_google_token(token)
    elif token and provider == "slack":
        revoke_slack_token(token)

    return {"disconnected": True}
