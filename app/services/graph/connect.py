"""Save one mailbox for the user who started OAuth.

The provider's code is exchanged before this runs. This function either commits
the new tokens or rolls back. It does not enqueue the worker.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.connected_account import ConnectedAccount
from app.models.user import utcnow
from app.services.mailboxes import lock_active_mailbox, lock_mailbox_owner


class ConnectStatus(str, Enum):
    connected = "connected"
    mailbox_taken = "mailbox_taken"
    mailbox_conflict = "mailbox_conflict"
    mailbox_mismatch = "mailbox_mismatch"
    not_saved = "not_saved"


@dataclass(frozen=True)
class ConnectOutcome:
    status: ConnectStatus
    account_id: UUID | None = None
    generation: int | None = None
    email: str | None = None


def _find_provider_account(
    session: Session, provider: str, provider_account_id: str
) -> ConnectedAccount | None:
    return session.exec(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.provider == provider,
            ConnectedAccount.provider_account_id == provider_account_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def connect_mailbox(
    session: Session,
    *,
    provider: str,
    user_id: UUID,
    provider_account_id: str,
    tenant_id: str | None,
    email: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: str | None,
    expected_mailbox_id: UUID | None = None,
    _retry: bool = False,
) -> ConnectOutcome:
    """Reconnect the same mailbox, or insert one for this provider.

    Gmail and Outlook can each have several active accounts. Slack allows one.
    Provider identity stays unique. An explicit reconnect must match its mailbox.
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
        if lock_mailbox_owner(session, user_id) is None:
            session.rollback()
            return ConnectOutcome(ConnectStatus.not_saved)
        existing = _find_provider_account(session, provider, provider_account_id)
        if existing is not None and existing.user_id != user_id:
            session.rollback()
            return ConnectOutcome(ConnectStatus.mailbox_taken)
        if expected_mailbox_id is not None and (existing is None or existing.id != expected_mailbox_id):
            session.rollback()
            return ConnectOutcome(ConnectStatus.mailbox_mismatch)
        if provider == "slack":
            active = lock_active_mailbox(session, user_id, provider=provider)
            if active is not None and (existing is None or active.id != existing.id):
                session.rollback()
                return ConnectOutcome(ConnectStatus.mailbox_conflict)
        now = utcnow()
        if existing is None:
            existing = ConnectedAccount(
                user_id=user_id,
                provider=provider,
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
                ConnectedAccount.provider == provider,
                ConnectedAccount.provider_account_id == provider_account_id,
            )
        ).first()
        if existing is not None and existing.user_id != user_id:
            session.rollback()
            return ConnectOutcome(ConnectStatus.mailbox_taken)
        return connect_mailbox(
            session,
            provider=provider,
            user_id=user_id,
            provider_account_id=provider_account_id,
            tenant_id=tenant_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
            expected_mailbox_id=expected_mailbox_id,
            _retry=True,
        )


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
    expected_mailbox_id: UUID | None = None,
    _retry: bool = False,
) -> ConnectOutcome:
    """Save a Microsoft mailbox. `provider_account_id` is the Entra `oid`."""
    return connect_mailbox(
        session,
        provider="microsoft",
        user_id=user_id,
        provider_account_id=provider_account_id,
        tenant_id=tenant_id,
        email=email,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
        expected_mailbox_id=expected_mailbox_id,
        _retry=_retry,
    )
