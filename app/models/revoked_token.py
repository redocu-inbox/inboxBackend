import app.db

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class RevokedToken(SQLModel, table=True):
    """A logged-out app JWT, identified by its jti, until that JWT expires."""

    __tablename__ = "revoked_tokens"

    jti: UUID = Field(primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
