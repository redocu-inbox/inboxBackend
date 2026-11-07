import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class PasswordResetRequest(SQLModel, table=True):
    """One forgot-password attempt. The raw code is never stored here."""

    __tablename__ = "password_reset_requests"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)


class PasswordResetToken(SQLModel, table=True):
    """SHA-256 of a reset code. The code itself is only in the outbound email."""

    __tablename__ = "password_reset_tokens"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    token_hash: str = Field(max_length=64, unique=True, index=True)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    used_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
