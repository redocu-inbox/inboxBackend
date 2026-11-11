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


class SyncStatusResponse(BaseModel):
    """Worker lifecycle for one mailbox. last_successful_at is only a finished sync."""

    status: Literal["idle", "pending", "running", "failed"]
    last_successful_at: datetime | None = None
    error_code: str | None = None


class MailboxResponse(BaseModel):
    """The connected mailbox. Tokens, scopes, and the delta link stay on the server."""

    connected: bool
    provider: str
    email: str


class MailboxEntryResponse(MailboxResponse):
    """One active mailbox and that mailbox's own sync status."""

    sync: SyncStatusResponse


class UserResponse(BaseModel):
    """Public profile fields. Keeps hashed_password out of every response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime
    mailbox: MailboxResponse | None = None
    mailboxes: list[MailboxEntryResponse] = Field(default_factory=list)
    sync: SyncStatusResponse | None = None


class UpdateProfileRequest(BaseModel):
    # Email is not editable until there is a way to verify a new address.
    # extra="forbid" responds 422 instead of silently ignoring an email field.
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=256)


def active_mailboxes(session: Session, user_id: UUID) -> list[ConnectedAccount]:
    """Every active mailbox. Outlook and Gmail can both be present."""
    return list(
        session.exec(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.disconnected_at.is_(None),
            )
            .order_by(ConnectedAccount.provider, ConnectedAccount.email)
        ).all()
    )


def _sync_response(account: ConnectedAccount) -> SyncStatusResponse:
    status = account.sync_status if account.sync_status in SYNC_STATUSES else "idle"
    return SyncStatusResponse(
        status=status,
        last_successful_at=account.last_sync_at,
        error_code=account.sync_error_code,
    )


def _combined_sync(accounts: list[ConnectedAccount]) -> SyncStatusResponse | None:
    """One status for the inbox. A failure or a running job is not hidden by an idle mailbox."""
    if not accounts:
        return None
    rank = {"failed": 3, "running": 2, "pending": 1, "idle": 0}
    chosen = accounts[0]
    for account in accounts[1:]:
        current = account.sync_status if account.sync_status in SYNC_STATUSES else "idle"
        best = chosen.sync_status if chosen.sync_status in SYNC_STATUSES else "idle"
        if rank[current] > rank[best]:
            chosen = account
    status = _sync_response(chosen)
    stamps = [account.last_sync_at for account in accounts if account.last_sync_at is not None]
    latest = max(stamps) if stamps else None
    return SyncStatusResponse(
        status=status.status,
        last_successful_at=latest,
        error_code=status.error_code if status.status == "failed" else None,
    )


def _profile_mailbox(accounts: list[ConnectedAccount]) -> ConnectedAccount | None:
    """The mailbox an existing client treats as Outlook.

    Prefer the Microsoft row when one is connected. A newer Gmail row must not
    take this field, or that client labels the Gmail address as Outlook and
    Disconnect clears the Outlook tokens.
    """
    for account in accounts:
        if account.provider == "microsoft":
            return account
    if not accounts:
        return None
    return accounts[0]


def _mailbox(account: ConnectedAccount) -> MailboxResponse:
    return MailboxResponse(connected=True, provider=account.provider, email=account.email)


def _mailbox_entry(account: ConnectedAccount) -> MailboxEntryResponse:
    return MailboxEntryResponse(
        connected=True,
        provider=account.provider,
        email=account.email,
        sync=_sync_response(account),
    )


def _user_response(user: User, session: Session) -> UserResponse:
    accounts = active_mailboxes(session, user.id)
    chosen = _profile_mailbox(accounts)
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        updated_at=user.updated_at,
        mailbox=_mailbox(chosen) if chosen is not None else None,
        mailboxes=[_mailbox_entry(account) for account in accounts],
        sync=_combined_sync(accounts),
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
