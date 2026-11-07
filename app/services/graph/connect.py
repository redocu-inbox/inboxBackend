"""Save one Microsoft mailbox for the user who started OAuth.

The Microsoft code is exchanged before this runs. This function either commits
the new tokens or rolls back. It does not enqueue the worker.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.user import User, utcnow


class ConnectStatus(str, Enum):
    connected = "connected"
    mailbox_taken = "mailbox_taken"
    mailbox_conflict = "mailbox_conflict"
    not_saved = "not_saved"


@dataclass(frozen=True)
class ConnectOutcome:
    status: ConnectStatus
    account_id: UUID | None = None
    generation: int | None = None
    email: str | None = None


def _lock_user(session: Session, user_id: UUID) -> User | None:
    return session.exec(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    ).first()


def _find_oid(session: Session, provider_account_id: str) -> ConnectedAccount | None:
    return session.exec(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.provider_account_id == provider_account_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _find_active(session: Session, user_id: UUID) -> ConnectedAccount | None:
    return session.exec(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.user_id == user_id,
            ConnectedAccount.provider == "microsoft",
            ConnectedAccount.disconnected_at.is_(None),
        )
        .order_by(ConnectedAccount.updated_at.desc(), ConnectedAccount.id.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def connect_microsoft_mailbox(
    session: Session,
    *,
    user_id: UUID,
    provider_account_id: str,
    tenant_id: str | None,
    email: str,
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    scopes: str | None,
    _retry: bool = False,
) -> ConnectOutcome:
    """Reconnect the same Microsoft account, or insert the first one.

    A different Microsoft account is rejected while one is still active.
    An oid owned by another app user is rejected. Neither rejection changes
    the stored tokens. A unique-index race is rolled back and classified
    instead of being left half-saved.
    """
    if (
        not provider_account_id
        or len(provider_account_id) > 128
        or not email
        or len(email) > 320
        or any(char in email for char in "\r\n\x00")
    ):
        return ConnectOutcome(ConnectStatus.not_saved)
    if tenant_id is not None and len(tenant_id) > 128:
        tenant_id = tenant_id[:128]
    try:
        if _lock_user(session, user_id) is None:
            session.rollback()
            return ConnectOutcome(ConnectStatus.not_saved)
        existing = _find_oid(session, provider_account_id)
        active = _find_active(session, user_id)
        if existing is not None and existing.user_id != user_id:
            session.rollback()
            return ConnectOutcome(ConnectStatus.mailbox_taken)
        if active is not None and (existing is None or active.id != existing.id):
            session.rollback()
            return ConnectOutcome(ConnectStatus.mailbox_conflict)
        now = utcnow()
        if existing is None:
            existing = ConnectedAccount(
                user_id=user_id,
                provider="microsoft",
                provider_account_id=provider_account_id,
                tenant_id=tenant_id,
                email=email,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expires_at=expires_at,
                scopes=scopes,
                sync_generation=1,
                sync_status="pending",
                sync_error_code=None,
                sync_status_at=now,
                disconnected_at=None,
            )
            session.add(existing)
        else:
            existing.email = email
            existing.tenant_id = tenant_id
            existing.disconnected_at = None
            existing.access_token = access_token
            existing.refresh_token = refresh_token
            existing.token_expires_at = expires_at
            existing.scopes = scopes
            existing.sync_generation = int(existing.sync_generation or 0) + 1
            existing.sync_status = "pending"
            existing.sync_error_code = None
            existing.sync_status_at = now
            existing.updated_at = now
            session.add(existing)
        session.commit()
        session.refresh(existing)
        return ConnectOutcome(
            ConnectStatus.connected,
            account_id=existing.id,
            generation=int(existing.sync_generation),
            email=existing.email,
        )
    except IntegrityError:
        session.rollback()
        if _retry:
            return ConnectOutcome(ConnectStatus.not_saved)
        existing = session.exec(
            select(ConnectedAccount).where(
                ConnectedAccount.provider == "microsoft",
                ConnectedAccount.provider_account_id == provider_account_id,
            )
        ).first()
        active = session.exec(
            select(ConnectedAccount).where(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.provider == "microsoft",
                ConnectedAccount.disconnected_at.is_(None),
            )
        ).first()
        if existing is not None and existing.user_id != user_id:
            return ConnectOutcome(ConnectStatus.mailbox_taken)
        if active is not None and (existing is None or active.id != existing.id):
            return ConnectOutcome(ConnectStatus.mailbox_conflict)
        return connect_microsoft_mailbox(
            session,
            user_id=user_id,
            provider_account_id=provider_account_id,
            tenant_id=tenant_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
            _retry=True,
        )
