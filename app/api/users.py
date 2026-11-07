"""Routes for the logged-in user's own profile."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_session
from app.models.connected_account import ConnectedAccount
from app.models.user import User, utcnow
from app.services.mailboxes import SYNC_STATUSES

router = APIRouter(prefix="/users", tags=["users"])


class MailboxResponse(BaseModel):
    """The connected mailbox. Tokens, scopes, and the delta link stay on the server."""

    connected: bool
    provider: str
    email: str


class SyncStatusResponse(BaseModel):
    """Worker lifecycle for the active mailbox. last_successful_at is only a finished sync."""

    status: Literal["idle", "pending", "running", "failed"]
    last_successful_at: datetime | None = None
    error_code: str | None = None


class UserResponse(BaseModel):
    """Public profile fields. Keeps hashed_password out of every response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    mailbox: MailboxResponse | None = None
    sync: SyncStatusResponse | None = None


class UpdateProfileRequest(BaseModel):
    # Email is not editable until there is a way to verify a new address.
    # extra="forbid" responds 422 instead of silently ignoring an email field.
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=256)


def latest_connected_account(session: Session, user_id: UUID) -> ConnectedAccount | None:
    """The user's active Microsoft mailbox. Disconnected rows are kept but hidden."""
    return session.exec(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.disconnected_at.is_(None),
        )
        .order_by(ConnectedAccount.updated_at.desc(), ConnectedAccount.id.desc())
    ).first()


def _sync_response(account: ConnectedAccount) -> SyncStatusResponse:
    status = account.sync_status if account.sync_status in SYNC_STATUSES else "idle"
    return SyncStatusResponse(
        status=status,
        last_successful_at=account.last_sync_at,
        error_code=account.sync_error_code,
    )


def _user_response(user: User, session: Session) -> UserResponse:
    account = latest_connected_account(session, user.id)
    mailbox = None
    sync = None
    if account is not None:
        mailbox = MailboxResponse(
            connected=True,
            provider=account.provider,
            email=account.email,
        )
        sync = _sync_response(account)
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        updated_at=user.updated_at,
        mailbox=mailbox,
        sync=sync,
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
