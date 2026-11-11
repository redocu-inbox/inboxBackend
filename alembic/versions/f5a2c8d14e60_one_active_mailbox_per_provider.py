"""one active mailbox per provider

Revision ID: f5a2c8d14e60
Revises: e4c1a9b82d30
Create Date: 2026-11-11 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.mailboxes import ACTIVE_GOOGLE_MAILBOX_INDEX, ONE_ACTIVE_MAILBOX_INDEX


revision: str = "f5a2c8d14e60"
down_revision: Union[str, Sequence[str], None] = "e4c1a9b82d30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Outlook and Gmail may both be active. One account per provider stays unique.
    op.drop_index(ONE_ACTIVE_MAILBOX_INDEX, table_name="connected_accounts")
    op.create_index(
        ACTIVE_GOOGLE_MAILBOX_INDEX,
        "connected_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("disconnected_at IS NULL AND provider = 'google'"),
    )


def downgrade() -> None:
    op.drop_index(ACTIVE_GOOGLE_MAILBOX_INDEX, table_name="connected_accounts")
    op.create_index(
        ONE_ACTIVE_MAILBOX_INDEX,
        "connected_accounts",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("disconnected_at IS NULL"),
    )
