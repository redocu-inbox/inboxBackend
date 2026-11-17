"""conversation, recipients, and staged files

Revision ID: b8e1c4d72a90
Revises: a4c9e2b71d08
Create Date: 2026-11-17 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8e1c4d72a90"
down_revision: Union[str, Sequence[str], None] = "a4c9e2b71d08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("conversation_id", sa.String(length=512), nullable=True))
    op.add_column("messages", sa.Column("to_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("messages", sa.Column("cc_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("messages", sa.Column("reply_to", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "messages",
        sa.Column("direction", sa.String(length=16), nullable=False, server_default="incoming"),
    )
    op.add_column(
        "messages",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="provider"),
    )
    op.add_column("messages", sa.Column("forward_of_id", sa.Uuid(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("material_attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_foreign_key(
        "fk_messages_forward_of_id",
        "messages",
        "messages",
        ["forward_of_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("reply_drafts", sa.Column("to_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("reply_drafts", sa.Column("cc_recipients", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_table(
        "staged_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staged_attachments_user_id", "staged_attachments", ["user_id"])
    op.create_index("ix_staged_attachments_message_id", "staged_attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_staged_attachments_message_id", table_name="staged_attachments")
    op.drop_index("ix_staged_attachments_user_id", table_name="staged_attachments")
    op.drop_table("staged_attachments")
    op.drop_column("reply_drafts", "cc_recipients")
    op.drop_column("reply_drafts", "to_recipients")
    op.drop_constraint("fk_messages_forward_of_id", "messages", type_="foreignkey")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_column("messages", "material_attachments")
    op.drop_column("messages", "forward_of_id")
    op.drop_column("messages", "origin")
    op.drop_column("messages", "direction")
    op.drop_column("messages", "reply_to")
    op.drop_column("messages", "cc_recipients")
    op.drop_column("messages", "to_recipients")
    op.drop_column("messages", "conversation_id")
