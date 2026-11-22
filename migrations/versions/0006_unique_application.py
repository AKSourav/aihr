"""one application per candidate and job

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-22

"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CONSTRAINT = "applications_org_id_candidate_id_job_id_key"


def upgrade() -> None:
    op.create_unique_constraint(
        CONSTRAINT, "applications", ["org_id", "candidate_id", "job_id"]
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "applications", type_="unique")
