"""fill job template nullable

Revision ID: a4c9e2b71d08
Revises: e7b1c4a92d50
Create Date: 2026-11-16 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4c9e2b71d08"
down_revision: Union[str, Sequence[str], None] = "e7b1c4a92d50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "form_fill_jobs",
        "template_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "form_fill_jobs",
        "template_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
