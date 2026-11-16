import app.db

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.user import utcnow

class FormFillStatus(str, Enum):
    pending= "pending"
    running= "running"
    needs_review = "needs_review"
    completed = "completed"
    failed = "failed"

class FormFillJob(SQLModel, table=True):
    __tablename__ = "form_fill_jobs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    template_id: UUID | None = Field(
        default=None, foreign_key="form_templates.id", index=True, ondelete="CASCADE"
    )
    message_id: UUID | None = Field(default=None, foreign_key="messages.id", index=True, ondelete="SET NULL")
    # Plain string, not a Postgres ENUM, adding a status later stays a normal migration.
    status: FormFillStatus = Field(
        default=FormFillStatus.pending,
        sa_column=Column(String(32), nullable=False, server_default="pending"),
    )
    confidence: float | None = Field(default=None)
    extracted_values: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    dropped_fields: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    filled_file_path: str | None = Field(default=None, max_length=1024)
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)