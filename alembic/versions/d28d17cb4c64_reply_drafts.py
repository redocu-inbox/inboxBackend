"""reply drafts

Revision ID: d28d17cb4c64
Revises: d9a7c4e16f32
Create Date: 2026-11-14 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d28d17cb4c64"
down_revision: Union[str, Sequence[str], None] = "d9a7c4e16f32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("graph_draft_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_reply_drafts_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_reply_drafts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reply_drafts")),
        sa.UniqueConstraint("message_id", name=op.f("uq_reply_drafts_message_id")),
    )
    op.create_index(op.f("ix_reply_drafts_user_id"), "reply_drafts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reply_drafts_user_id"), table_name="reply_drafts")
    op.drop_table("reply_drafts")
