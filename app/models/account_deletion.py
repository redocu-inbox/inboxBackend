"""Durable requests awaiting account cleanup."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class AccountDeletion(SQLModel, table=True):
    __tablename__ = "account_deletions"

    # The request must survive removal of the user row during cleanup.
    user_id: UUID = Field(primary_key=True)
    requested_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
    local_purged_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
