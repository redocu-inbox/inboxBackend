"""fill job dropped fields

Revision ID: e7b1c4a92d50
Revises: d28d17cb4c64
Create Date: 2026-11-16 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e7b1c4a92d50"
down_revision: Union[str, Sequence[str], None] = "d28d17cb4c64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "form_fill_jobs",
        sa.Column(
            "dropped_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("form_fill_jobs", "dropped_fields")
