"""Forgot-password requests and the single-use reset transaction.

The queue job receives a user id and a request id. It creates the code in
memory, stores only the SHA-256 hash, and then asks the mail sender to deliver
the link. A retry creates a new code and invalidates the unused one. The raw
code is never written to the database, the job arguments, or a log line.
"""

import hashlib
import logging
import secrets
from datetime import timedelta
from urllib.parse import quote
from uuid import UUID

from sqlmodel import Session, select

from app.config import get_settings
from app.models.password_reset import PasswordResetRequest, PasswordResetToken
from app.models.user import User, utcnow
from app.security import hash_password
from app.services.account_deletion import lock_account_write
from app.services.email.base import EmailDeliveryError, EmailMessage
from app.services.email.factory import get_email_sender

logger = logging.getLogger(__name__)

RESET_TTL = timedelta(minutes=30)
SUBJECT = "Reset your redocu password"
_BODY = (
    "Someone asked to reset the password for your redocu account. "
    "Use this link within 30 minutes: {link}. "
    "If it wasn't you, ignore this email; your password hasn't changed."
)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def reset_link(raw_token: str) -> str:
    origin = get_settings().frontend_url.rstrip("/")
    return f"{origin}/reset-password#token={quote(raw_token, safe='')}"


def open_reset_request(session: Session, user: User) -> UUID | None:
    """Invalidate unused codes and record a new request. Does not create a code."""
    if not lock_account_write(session, user.id):
        return None
    locked = session.get(User, user.id, with_for_update=True, populate_existing=True)
    if locked is None:
        return None
    now = utcnow()
    unused = session.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == locked.id,
            PasswordResetToken.used_at.is_(None),
        )
    ).all()
    for row in unused:
        row.used_at = now
        session.add(row)
    request = PasswordResetRequest(user_id=locked.id)
    session.add(request)
    session.commit()
    return request.id


def deliver_password_reset(session: Session, user_id: UUID, request_id: UUID) -> None:
    """Create a hash, then send the link. Stale requests send nothing."""
    if not lock_account_write(session, user_id):
        return
    locked_user = session.exec(select(User).where(User.id == user_id).with_for_update()).first()
    request = session.get(PasswordResetRequest, request_id)
    if locked_user is None or request is None or request.user_id != locked_user.id:
        session.rollback()
        return
    latest = session.exec(
        select(PasswordResetRequest)
        .where(PasswordResetRequest.user_id == locked_user.id)
        .order_by(PasswordResetRequest.created_at.desc(), PasswordResetRequest.id.desc())
    ).first()
    if latest is None or latest.id != request.id:
        session.rollback()
        return
    now = utcnow()
    unused = session.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == locked_user.id,
            PasswordResetToken.used_at.is_(None),
        )
    ).all()
    for row in unused:
        row.used_at = now
        session.add(row)
    raw_token = secrets.token_urlsafe(32)
    session.add(
        PasswordResetToken(
            user_id=locked_user.id,
            token_hash=token_hash(raw_token),
            expires_at=now + RESET_TTL,
        )
    )
    email = locked_user.email
    session.commit()
    if not lock_account_write(session, user_id):
        return
    message = EmailMessage(
        to=email,
        subject=SUBJECT,
        text=_BODY.format(link=reset_link(raw_token)),
    )
    try:
        get_email_sender().send(message)
    except EmailDeliveryError:
        logger.warning("password reset delivery failed")
        raise
    except Exception as exc:
        logger.warning("password reset delivery failed error_type=%s", type(exc).__name__)
        raise EmailDeliveryError("status=error") from None


def consume_reset_token(session: Session, raw_token: str, new_password: str) -> bool:
    """Set the new password and bump session_version, or return False.

    The token row and the user row are locked in this transaction. A second
    caller blocks, then sees used_at and fails. session_version rejects every
    access token minted with the previous version, including legacy tokens
    that have no ver claim and tokens that share an iat second.
    The Argon2 hash is computed before the lock so a slow hash does not hold it.
    Argon2 is not truncated at 72 bytes; the 8–128 character rule is the same
    one registration uses.
    """
    password_hash = hash_password(new_password)
    row = session.exec(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash(raw_token))
        .with_for_update()
    ).first()
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at <= now:
        session.rollback()
        return False
    if not lock_account_write(session, row.user_id):
        return False
    user = session.get(User, row.user_id, with_for_update=True, populate_existing=True)
    if user is None:
        return False
    row.used_at = now
    user.hashed_password = password_hash
    user.session_version = int(user.session_version or 0) + 1
    user.updated_at = now
    session.add(row)
    session.add(user)
    session.commit()
    return True
