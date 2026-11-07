"""Shared FastAPI dependencies for auth, database sessions, and services."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.db import engine
from app.models.revoked_token import RevokedToken
from app.models.user import User, utcnow
from app.security import decode_access_token

# auto_error=False so a missing header gets the same 401 as a bad token.
_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    """Yield one database session and close it when the request ends."""
    with Session(engine) as session:
        yield session


def _token_is_revoked(session: Session, claims) -> bool:
    """True when this token was logged out.

    Tokens with a jti are revoked one at a time. Legacy tokens without a jti
    are revoked only when their iat is at or before the user's auth_valid_after,
    which logout sets from that legacy token. A later login's jti is not affected
    by auth_valid_after.
    """
    user = session.get(User, claims.user_id)
    if user is None:
        return True
    if claims.jti is not None:
        row = session.get(RevokedToken, claims.jti)
        return row is not None and row.expires_at > utcnow()
    if user.auth_valid_after is None:
        return False
    return claims.issued_at <= user.auth_valid_after


def get_current_user(
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Return the user from the Bearer token, or respond 401."""
    claims = decode_access_token(credentials.credentials) if credentials else None
    # The user lookup rejects valid tokens that belong to a deleted account.
    user = session.get(User, claims.user_id) if claims else None
    if user is None or (claims is not None and _token_is_revoked(session, claims)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_arq(request: Request):
    """Return the ARQ pool created in the API lifespan."""
    return request.app.state.arq
