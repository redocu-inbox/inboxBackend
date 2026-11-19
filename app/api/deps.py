"""Shared FastAPI dependencies for auth, database sessions, and services."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.db import engine
from app.models.revoked_token import RevokedToken
from app.models.user import User, utcnow
from app.security import AccessTokenClaims, decode_access_token
from app.services.account_deletion import (
    account_is_deleting,
    lock_account_request,
    try_lock_deletion,
    unlock_account_request,
)

# auto_error=False so a missing header gets the same 401 as a bad token.
_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    """Yield one database session and close it when the request ends."""
    # Keep the connection checked out across commits so the request lock stays
    # with this request and can be released before the connection returns to the pool.
    with engine.connect() as connection:
        session = Session(connection)
        try:
            with session:
                yield session
        finally:
            lock_key = session.info.pop("account_request_lock", None)
            if lock_key is not None:
                unlock_account_request(connection, lock_key)


def _token_is_revoked(session: Session, user: User, claims: AccessTokenClaims) -> bool:
    """True when this token was logged out.

    Tokens with a jti are revoked one at a time. Legacy tokens without a jti
    are revoked only when their iat is at or before the user's auth_valid_after,
    which logout sets from that legacy token. A later login's jti is not affected
    by auth_valid_after.
    """
    token_version = 0 if claims.session_version is None else claims.session_version
    if token_version != int(user.session_version or 0):
        return True
    if claims.jti is not None:
        row = session.get(RevokedToken, claims.jti)
        return row is not None and row.expires_at > utcnow()
    if user.auth_valid_after is None:
        return False
    return claims.issued_at <= user.auth_valid_after


def _authenticated_user(session: Session, claims: AccessTokenClaims | None, *, allow_deleting: bool = False) -> User:
    user = session.get(User, claims.user_id) if claims else None
    if (
        claims is None
        or user is None
        or _token_is_revoked(session, user, claims)
        or (not allow_deleting and account_is_deleting(session, user.id))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_user(
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Return an active user from the Bearer token, or respond 401."""
    claims = decode_access_token(credentials.credentials) if credentials else None
    if claims is not None:
        lock_account_request(session, claims.user_id)
    return _authenticated_user(session, claims)


def get_deletion_user(
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Allow a valid token to repeat its deletion request after writes are blocked."""
    claims = decode_access_token(credentials.credentials) if credentials else None
    locked = claims is not None and try_lock_deletion(session, claims.user_id)
    user = _authenticated_user(session, claims, allow_deleting=True)
    if not locked and not account_is_deleting(session, user.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account activity is in progress. Retry deletion shortly.",
            headers={"Retry-After": "1"},
        )
    return user


def get_arq(request: Request):
    """Return the ARQ pool created in the API lifespan."""
    return request.app.state.arq
