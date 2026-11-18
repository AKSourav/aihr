"""email invites on calls

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17

"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("invite_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calls", sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "invite_expires_at")
    op.drop_column("calls", "invite_sent_at")
