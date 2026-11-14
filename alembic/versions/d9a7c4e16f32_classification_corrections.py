"""classification correction log

Revision ID: d9a7c4e16f32
Revises: c7e5a2b94d10
Create Date: 2026-11-13 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9a7c4e16f32"
down_revision: Union[str, Sequence[str], None] = "c7e5a2b94d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_classification_corrections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_classification_corrections_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_message_classification_corrections_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_classification_corrections")),
    )
    op.create_index(
        op.f("ix_message_classification_corrections_message_id"),
        "message_classification_corrections",
        ["message_id"],
        unique=False,
    )
    op.create_check_constraint(
        "field",
        "message_classification_corrections",
        "field IN ('category', 'project_name', 'project_number', 'due_date', "
        "'responsible_party', 'reference_number')",
    )


def downgrade() -> None:
    op.drop_table("message_classification_corrections")
