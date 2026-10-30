import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.user import utcnow

class ConnectedAccount(SQLModel, table=True):
    """Outlook / Microsft Graph credentials for one mailbox."""

    __tablename__ = "connected_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_connected_accounts_provider_account"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    provider: str = Field(default="microsoft", max_length=32)
    # Microsoft Entra object id (`oid`). Stable across token refreshes.
    provider_account_id: str = Field(max_length=128)
    tenant_id: str | None = Field(default=None, max_length=128)
    email: str = Field(max_length=320)

    # Graph tokens are long. VARCHAR(255) will truncate them and break refresh.
    # Still plaintext in this phase. Do not log these columns.

    access_token: str = Field(sa_column=Column(Text, nullable=False))
    refresh_token: str | None = Field(default=None, sa_column=Column(Text))
    token_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    scopes: str | None = Field(default=None, sa_column=Column(Text))
    delta_link: str | None = Field(default=None, sa_column=Column(Text))
    graph_subscription_id: str | None = Field(default=None,max_length=128)
    graph_subscription_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))

    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))