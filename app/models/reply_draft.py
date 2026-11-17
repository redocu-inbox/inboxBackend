"""One local reply draft per message.

`created_at` stays on the row. The API object does not include it.
The status column is a plain string so a later status is a normal migration.
"""

import app.db

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class ReplyDraftStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    failed = "failed"


class ReplyDraft(SQLModel, table=True):
    __tablename__ = "reply_drafts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    message_id: UUID = Field(foreign_key="messages.id", unique=True, ondelete="CASCADE")
    # Plain string, not a Postgres ENUM.
    status: ReplyDraftStatus = Field(
        default=ReplyDraftStatus.pending,
        sa_column=Column(String(32), nullable=False, server_default="pending"),
    )
    body: str | None = Field(default=None, sa_column=Column(Text))
    source: str | None = Field(default=None, max_length=16)
    graph_draft_id: str | None = Field(default=None, sa_column=Column(Text))
    error: str | None = Field(default=None, sa_column=Column(Text))
    # Saved outgoing To/Cc. Null means the reply still uses the sender or Reply-To.
    to_recipients: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    cc_recipients: list | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )
