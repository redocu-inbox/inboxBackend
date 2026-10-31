"""Routes for the logged-in user's own profile."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.api.deps import get_current_user, get_session
from app.models.user import User, utcnow

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(BaseModel):
    """Public profile fields. Keeps hashed_password out of every response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime


class UpdateProfileRequest(BaseModel):
    # Email is not editable until there is a way to verify a new address.
    # extra="forbid" responds 422 instead of silently ignoring an email field.
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=256)


@router.get("/me")
def read_me(user: User = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)


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
    return UserResponse.model_validate(user)
