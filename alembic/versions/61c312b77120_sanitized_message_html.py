"""Store sanitized HTML separately from the plain-text message body."""

import sqlalchemy as sa
from alembic import op

revision = "61c312b77120"
down_revision = "28bb091baba5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("body_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "body_html")
