"""A file staged from the user's computer, separate from form templates.

Unbound rows expire 24 hours after upload. Binding one to a message keeps it.
"""

import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class StagedAttachment(SQLModel, table=True):
    __tablename__ = "staged_attachments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    message_id: UUID | None = Field(default=None, foreign_key="messages.id", index=True, ondelete="CASCADE")
    file_name: str = Field(sa_column=Column(Text, nullable=False))
    size_bytes: int
    content_type: str | None = Field(default=None, max_length=255)
    storage_key: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_type=DateTime(timezone=True),
        nullable=False,
    )
