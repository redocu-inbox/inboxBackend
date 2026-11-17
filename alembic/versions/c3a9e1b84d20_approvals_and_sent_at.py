"""approvals and message sent timestamp

Revision ID: c3a9e1b84d20
Revises: b8e1c4d72a90
Create Date: 2026-11-17 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3a9e1b84d20"
down_revision: Union[str, Sequence[str], None] = "b8e1c4d72a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by_name", sa.Text(), nullable=True),
        sa.Column("approved_by_email", sa.String(length=320), nullable=False),
        sa.Column("sent_from", sa.String(length=320), nullable=True),
        sa.Column("sent_to", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("form", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_user_id", "approvals", ["user_id"])
    op.create_index("ix_approvals_message_id", "approvals", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_message_id", table_name="approvals")
    op.drop_index("ix_approvals_user_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_column("messages", "approved_at")
