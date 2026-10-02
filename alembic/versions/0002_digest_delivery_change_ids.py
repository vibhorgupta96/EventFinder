"""record the EventChanges represented by each digest chunk

Revision ID: 0002_digest_delivery_change_ids
Revises: 0001_initial
Create Date: 2026-10-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_digest_delivery_change_ids"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows cannot be assigned accurately from the schema alone.
    op.add_column("digest_deliveries", sa.Column("event_change_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("digest_deliveries", "event_change_ids")
