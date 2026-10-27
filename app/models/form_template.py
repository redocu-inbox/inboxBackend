import app.db 

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, Text, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.user import utcnow

class FormTemplate(SQLModel, table=True):
    __tablename__ = "form_templates"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    filename: str = Field(max_length=512)
    # Key passed to ObjectStorage.put/get, not an absolute host path.
    storage_path: str = Field(max_length=1024)
    mime_type: str = Field(max_length=255)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text))
    # Column is schema_json. "schema" is easy to confuse with the postgres schema.
    form_schema: dict = Field(
        default_factory = dict,
        sa_column=Column("schema_json", JSONB, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
    updated_at: datetime= Field(default_factory=utcnow, sa_type=DateTime(timezone=True), nullable=False)
