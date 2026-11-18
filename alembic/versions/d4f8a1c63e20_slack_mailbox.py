"""slack mailbox

Revision ID: d4f8a1c63e20
Revises: c3a9e1b84d20
Create Date: 2026-11-18 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.mailboxes import ACTIVE_SLACK_MAILBOX_INDEX


revision: str = "d4f8a1c63e20"
down_revision: Union[str, Sequence[str], None] = "c3a9e1b84d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("slack_channel_id", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("slack_thread_ts", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("slack_permalink", sa.Text(), nullable=True))
    op.create_index(
        ACTIVE_SLACK_MAILBOX_INDEX,
        "connected_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("disconnected_at IS NULL AND provider = 'slack'"),
    )
    op.create_table(
        "slack_sync_cursors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connected_account_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("last_ts", sa.String(length=32), nullable=True),
        sa.Column("next_cursor", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connected_account_id"],
            ["connected_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connected_account_id",
            "channel_id",
            name="uq_slack_sync_cursors_channel",
        ),
    )
    op.create_index(
        "ix_slack_sync_cursors_connected_account_id",
        "slack_sync_cursors",
        ["connected_account_id"],
    )
    op.create_table(
        "slack_user_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connected_account_id", sa.Uuid(), nullable=False),
        sa.Column("slack_user_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connected_account_id"],
            ["connected_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connected_account_id",
            "slack_user_id",
            name="uq_slack_user_cache_user",
        ),
    )
    op.create_index(
        "ix_slack_user_cache_connected_account_id",
        "slack_user_cache",
        ["connected_account_id"],
    )
    op.create_table(
        "slack_sent_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connected_account_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("thread_ts", sa.String(length=32), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("slack_ts", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connected_account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_slack_sent_messages_user_id", "slack_sent_messages", ["user_id"])
    op.create_index(
        "ix_slack_sent_messages_connected_account_id",
        "slack_sent_messages",
        ["connected_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_slack_sent_messages_connected_account_id", table_name="slack_sent_messages")
    op.drop_index("ix_slack_sent_messages_user_id", table_name="slack_sent_messages")
    op.drop_table("slack_sent_messages")
    op.drop_index("ix_slack_user_cache_connected_account_id", table_name="slack_user_cache")
    op.drop_table("slack_user_cache")
    op.drop_index("ix_slack_sync_cursors_connected_account_id", table_name="slack_sync_cursors")
    op.drop_table("slack_sync_cursors")
    op.drop_index(ACTIVE_SLACK_MAILBOX_INDEX, table_name="connected_accounts")
    op.drop_column("messages", "slack_permalink")
    op.drop_column("messages", "slack_thread_ts")
    op.drop_column("messages", "slack_channel_id")
