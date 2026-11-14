import app.db

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, Date, DateTime, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel, Field

from app.models.user import utcnow

class Message(SQLModel, table=True):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("connected_account_id", "graph_id", name="uq_messages_account_graph_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    connected_account_id: UUID = Field(
        foreign_key="connected_accounts.id", index=True, ondelete="CASCADE"
    )
    graph_id: str = Field(max_length=512)
    subject: str | None = Field(default=None, sa_column=Column(Text))
    body_preview: str | None = Field(default=None, sa_column=Column(Text))
    # Plain text only. HTML from Graph is converted before it is stored.
    from_name: str | None = Field(default=None, sa_column=Column(Text))
    from_email: str | None = Field(default=None, max_length=320)
    is_read: bool | None = Field(default=None)
    body_text: str | None = Field(default=None, sa_column=Column(Text))
    received_at: datetime = Field(sa_type=DateTime(timezone=True), index=True)
    priority_score: float | None = Field(default=None, index=True)
    # Null until a scorer writes them. priority_scored_at is not returned by the API.
    priority_band: str | None = Field(default=None, max_length=16)
    priority_reasons: list[Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    priority_scored_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    category: str | None = Field(default=None, max_length=32)
    category_source: str | None = Field(default=None, max_length=16)
    category_confidence: float | None = Field(default=None)
    project_name: str | None = Field(default=None, sa_column=Column(Text))
    project_number: str | None = Field(default=None, max_length=64)
    due_date: date | None = Field(default=None, sa_type=Date)
    responsible_party: str | None = Field(default=None, sa_column=Column(Text))
    reference_number: str | None = Field(default=None, max_length=64)
    # Null until a correction. The API returns false for null.
    user_corrected: bool | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))