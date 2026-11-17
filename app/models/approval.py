"""One row written when approve sends a draft. The approval page reads these."""

import app.db

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    message_id: UUID | None = Field(default=None, foreign_key="messages.id", index=True, ondelete="SET NULL")
    approved_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    approved_by_name: str | None = Field(default=None, sa_column=Column(Text))
    approved_by_email: str = Field(max_length=320)
    sent_from: str | None = Field(default=None, max_length=320)
    sent_to: str = Field(sa_column=Column(Text, nullable=False))
    subject: str | None = Field(default=None, sa_column=Column(Text))
    reply: str = Field(sa_column=Column(Text, nullable=False))
    form: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )
