"""User corrections to a message classification. These rows are the labeled set."""

import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text
from sqlmodel import Field, SQLModel

CLASSIFICATION_CATEGORIES = (
    "submittal",
    "rfi",
    "change_order",
    "invoice",
    "meeting_notes",
    "safety",
    "schedule",
    "other",
)

CLASSIFICATION_FIELDS = (
    "category",
    "project_name",
    "project_number",
    "due_date",
    "responsible_party",
    "reference_number",
)


class MessageClassificationCorrection(SQLModel, table=True):
    """One field change. Rows are inserts. The message keeps the corrected value."""

    __tablename__ = "message_classification_corrections"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    message_id: UUID = Field(foreign_key="messages.id", index=True, ondelete="CASCADE")
    user_id: UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    field: str = Field(max_length=32)
    original_value: str | None = Field(default=None, sa_column=Column(Text))
    corrected_value: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(sa_type=DateTime(timezone=True))
