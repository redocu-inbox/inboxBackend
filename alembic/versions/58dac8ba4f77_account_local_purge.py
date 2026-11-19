"""Record when account database and file cleanup finishes."""

import sqlalchemy as sa
from alembic import op

revision = "58dac8ba4f77"
down_revision = "ace5ab7f41b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_deletions", sa.Column("local_purged_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("account_deletions", "local_purged_at")
