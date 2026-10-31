"""Shared FastAPI dependencies for auth, database sessions, and services."""

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.db import engine
from app.models.user import User
from app.security import decode_access_token

# auto_error=False so a missing header gets the same 401 as a bad token.
_bearer = HTTPBearer(auto_error=False)


def get_session() -> Iterator[Session]:
    """Yield one database session and close it when the request ends."""
    with Session(engine) as session:
        yield session


def get_current_user(
    session: Session = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Return the user from the Bearer token, or respond 401."""
    user_id = decode_access_token(credentials.credentials) if credentials else None
    # The user lookup rejects valid tokens that belong to a deleted account.
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_arq(request: Request):
    """Return the ARQ pool created in the API lifespan."""
    return request.app.state.arq
