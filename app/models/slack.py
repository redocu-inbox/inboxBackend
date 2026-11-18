"""Slack sync cursors, display-name cache, and the send audit log."""

import app.db

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.user import utcnow


class SlackSyncCursor(SQLModel, table=True):
    """Last-seen Slack timestamp for one conversation on one connected account."""

    __tablename__ = "slack_sync_cursors"
    __table_args__ = (
        UniqueConstraint(
            "connected_account_id",
            "channel_id",
            name="uq_slack_sync_cursors_channel",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    connected_account_id: UUID = Field(
        foreign_key="connected_accounts.id", index=True, ondelete="CASCADE"
    )
    channel_id: str = Field(max_length=32)
    # im, mpim, or channel. Channel includes private channels.
    kind: str = Field(max_length=16)
    last_ts: str | None = Field(default=None, max_length=32)
    # JSON resume state while a backfill is still walking pages. Null when caught up.
    next_cursor: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class SlackUserCache(SQLModel, table=True):
    """Display name for a Slack user id, scoped to the token that fetched it."""

    __tablename__ = "slack_user_cache"
    __table_args__ = (
        UniqueConstraint(
            "connected_account_id",
            "slack_user_id",
            name="uq_slack_user_cache_user",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    connected_account_id: UUID = Field(
        foreign_key="connected_accounts.id", index=True, ondelete="CASCADE"
    )
    slack_user_id: str = Field(max_length=32)
    display_name: str = Field(sa_column=Column(Text))
    fetched_at: datetime = Field(sa_type=DateTime(timezone=True))


class SlackSentMessage(SQLModel, table=True):
    """One explicit send attempt. Sync and scoring never insert these."""

    __tablename__ = "slack_sent_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    connected_account_id: UUID = Field(
        foreign_key="connected_accounts.id", index=True, ondelete="CASCADE"
    )
    channel_id: str = Field(max_length=32)
    thread_ts: str | None = Field(default=None, max_length=32)
    text: str = Field(sa_column=Column(Text))
    slack_ts: str | None = Field(default=None, max_length=32)
    # pending, sent, or failed.
    status: str = Field(max_length=16)
    error: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
