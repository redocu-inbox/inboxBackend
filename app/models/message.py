import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
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
    received_at: datetime = Field(sa_type=DateTime(timezone=True), index=True)
    priority_score: float | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))