"""priority band, reasons, and classification columns on messages

Revision ID: c7e5a2b94d10
Revises: f5a2c8d14e60
Create Date: 2026-11-13 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c7e5a2b94d10"
down_revision: Union[str, Sequence[str], None] = "f5a2c8d14e60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CATEGORIES = (
    "submittal",
    "rfi",
    "change_order",
    "invoice",
    "meeting_notes",
    "safety",
    "schedule",
    "other",
)


def upgrade() -> None:
    # No server defaults. Existing rows stay unscored and unclassified.
    op.add_column("messages", sa.Column("priority_band", sa.String(length=16), nullable=True))
    op.add_column(
        "messages",
        sa.Column("priority_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("priority_scored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("messages", sa.Column("category", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("category_source", sa.String(length=16), nullable=True))
    op.add_column("messages", sa.Column("category_confidence", sa.Float(), nullable=True))
    op.add_column("messages", sa.Column("project_name", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("project_number", sa.String(length=64), nullable=True))
    op.add_column("messages", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("messages", sa.Column("responsible_party", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("reference_number", sa.String(length=64), nullable=True))
    op.add_column("messages", sa.Column("user_corrected", sa.Boolean(), nullable=True))
    # Names are the constraint_name token. The metadata convention renders
    # ck_messages_<name>, the same way primary keys render pk_messages.
    op.create_check_constraint(
        "priority_band",
        "messages",
        "priority_band IS NULL OR priority_band IN ('low', 'medium', 'high')",
    )
    categories = ", ".join(f"'{value}'" for value in _CATEGORIES)
    op.create_check_constraint(
        "category",
        "messages",
        f"category IS NULL OR category IN ({categories})",
    )
    op.create_check_constraint(
        "category_source",
        "messages",
        "category_source IS NULL OR category_source IN ('rule', 'user')",
    )
    op.create_check_constraint(
        "category_confidence",
        "messages",
        "category_confidence IS NULL OR (category_confidence >= 0 AND category_confidence <= 1)",
    )


def downgrade() -> None:
    op.drop_constraint("category_confidence", "messages", type_="check")
    op.drop_constraint("category_source", "messages", type_="check")
    op.drop_constraint("category", "messages", type_="check")
    op.drop_constraint("priority_band", "messages", type_="check")
    op.drop_column("messages", "user_corrected")
    op.drop_column("messages", "reference_number")
    op.drop_column("messages", "responsible_party")
    op.drop_column("messages", "due_date")
    op.drop_column("messages", "project_number")
    op.drop_column("messages", "project_name")
    op.drop_column("messages", "category_confidence")
    op.drop_column("messages", "category_source")
    op.drop_column("messages", "category")
    op.drop_column("messages", "priority_scored_at")
    op.drop_column("messages", "priority_reasons")
    op.drop_column("messages", "priority_band")
