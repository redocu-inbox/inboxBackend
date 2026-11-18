import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.user import utcnow
from app.security import EncryptedText

class ConnectedAccount(SQLModel, table=True):
    """Outlook, Gmail, or Slack credentials for one mailbox."""

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

    # Encrypted in the database and plaintext on this object. Do not log these columns.
    # Null after an explicit disconnect so a queued job cannot refresh access.
    access_token: str | None = Field(default=None, sa_column=Column(EncryptedText, nullable=True), repr=False)
    refresh_token: str | None = Field(default=None, sa_column=Column(EncryptedText), repr=False)
    token_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    scopes: str | None = Field(default=None, sa_column=Column(Text))
    delta_link: str | None = Field(default=None, sa_column=Column(Text))
    graph_subscription_id: str | None = Field(default=None,max_length=128)
    graph_subscription_expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))

    # Set when this row is not the user's active mailbox. Tokens may still be
    # present on rows deactivated by the one-mailbox migration. An explicit
    # disconnect clears the tokens as well.
    disconnected_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # idle | pending | running | failed. last_sync_at changes only after a full success.
    sync_status: str = Field(default="idle", max_length=16)
    sync_generation: int = Field(default=0)
    sync_status_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_sync_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    sync_error_code: str | None = Field(default=None, max_length=32)

    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))