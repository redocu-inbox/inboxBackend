"""one active mailbox, sync status, message text, and JWT revocation

Revision ID: b7d3e9a41c20
Revises: a1f4c7d29e10
Create Date: 2026-11-07 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.mailboxes import ACTIVE_MAILBOX_INDEX, deactivate_extra_microsoft_mailboxes


revision: str = "b7d3e9a41c20"
down_revision: Union[str, Sequence[str], None] = "a1f4c7d29e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auth_valid_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "connected_accounts",
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connected_accounts",
        sa.Column("sync_status", sa.String(length=16), nullable=False, server_default="idle"),
    )
    op.add_column(
        "connected_accounts",
        sa.Column("sync_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "connected_accounts",
        sa.Column("sync_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connected_accounts",
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connected_accounts",
        sa.Column("sync_error_code", sa.String(length=32), nullable=True),
    )
    op.alter_column("connected_accounts", "access_token", existing_type=sa.Text(), nullable=True)
    op.add_column("messages", sa.Column("from_name", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("from_email", sa.String(length=320), nullable=True))
    op.add_column("messages", sa.Column("is_read", sa.Boolean(), nullable=True))
    op.add_column("messages", sa.Column("body_text", sa.Text(), nullable=True))
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_revoked_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_revoked_tokens")),
    )
    op.create_index(op.f("ix_revoked_tokens_user_id"), "revoked_tokens", ["user_id"], unique=False)
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"], unique=False)
    # Mark older duplicate mailboxes inactive before the unique index. Tokens
    # and messages stay on those rows.
    deactivate_extra_microsoft_mailboxes(op.get_bind())
    op.create_index(
        ACTIVE_MAILBOX_INDEX,
        "connected_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("disconnected_at IS NULL AND provider = 'microsoft'"),
    )


def downgrade() -> None:
    op.drop_index(ACTIVE_MAILBOX_INDEX, table_name="connected_accounts")
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_index(op.f("ix_revoked_tokens_user_id"), table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
    op.drop_column("messages", "body_text")
    op.drop_column("messages", "is_read")
    op.drop_column("messages", "from_email")
    op.drop_column("messages", "from_name")
    op.execute("UPDATE connected_accounts SET access_token = '' WHERE access_token IS NULL")
    op.alter_column("connected_accounts", "access_token", existing_type=sa.Text(), nullable=False)
    op.drop_column("connected_accounts", "sync_error_code")
    op.drop_column("connected_accounts", "last_sync_at")
    op.drop_column("connected_accounts", "sync_status_at")
    op.drop_column("connected_accounts", "sync_generation")
    op.drop_column("connected_accounts", "sync_status")
    op.drop_column("connected_accounts", "disconnected_at")
    op.drop_column("users", "auth_valid_after")
