"""Shared FastAPI dependencies for auth, database sessions, and services."""

from collections.abc import Iterator
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlmodel import Session

from app.db import engine
from app.models.user import User


def get_session() -> Iterator[Session]:
    """Yield one database session and close it when the request ends."""
    with Session(engine) as session:
        yield session


def get_current_user(
    session: Session = Depends(get_session),
    user_id: UUID = Header(alias="X-User-Id"),
) -> User:
    """Replace this when real auth exists."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def get_arq(request: Request):
    """Return the ARQ pool created in the API lifespan."""
    return request.app.state.arq
