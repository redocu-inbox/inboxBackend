"""Routes for the logged-in user's own profile."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_session
from app.models.connected_account import ConnectedAccount
from app.models.user import User, utcnow

router = APIRouter(prefix="/users", tags=["users"])


class MailboxResponse(BaseModel):
    """The connected mailbox. Tokens, scopes, and the delta link stay on the server."""

    connected: bool
    provider: str
    email: str


class UserResponse(BaseModel):
    """Public profile fields. Keeps hashed_password out of every response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    mailbox: MailboxResponse | None = None


class UpdateProfileRequest(BaseModel):
    # Email is not editable until there is a way to verify a new address.
    # extra="forbid" responds 422 instead of silently ignoring an email field.
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=256)


def latest_connected_account(session: Session, user_id: UUID) -> ConnectedAccount | None:
    """The user's mailbox with the latest updated_at. id breaks a tie."""
    return session.exec(
        select(ConnectedAccount)
        .where(ConnectedAccount.user_id == user_id)
        .order_by(ConnectedAccount.updated_at.desc(), ConnectedAccount.id.desc())
    ).first()


def _user_response(user: User, session: Session) -> UserResponse:
    account = latest_connected_account(session, user.id)
    mailbox = None
    if account is not None:
        mailbox = MailboxResponse(
            connected=True,
            provider=account.provider,
            email=account.email,
        )
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        updated_at=user.updated_at,
        mailbox=mailbox,
    )


@router.get("/me")
def read_me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UserResponse:
    return _user_response(user, session)


@router.patch("/me")
def update_me(
    body: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UserResponse:
    # Omitting display_name leaves it unchanged. Sending null clears it.
    if "display_name" in body.model_fields_set:
        user.display_name = body.display_name
        user.updated_at = utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
    return _user_response(user, session)
