"""job shortlist rules, latest-score index and resume-scoring llm usage

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-26

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("shortlist_rules", sa.JSON(), nullable=True))

    op.create_index(
        "ix_scores_latest",
        "scores",
        ["application_id", "stage", sa.text("created_at DESC")],
    )

    op.add_column(
        "llm_usage", sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.alter_column("llm_usage", "call_id", existing_type=postgresql.UUID(), nullable=True)
    op.create_foreign_key(
        "fk_llm_usage_application",
        "llm_usage",
        "applications",
        ["application_id", "org_id"],
        ["id", "org_id"],
    )
    op.create_check_constraint(
        "ck_llm_usage_subject", "llm_usage", "num_nonnulls(call_id, application_id) = 1"
    )


def downgrade() -> None:
    op.drop_constraint("ck_llm_usage_subject", "llm_usage", type_="check")
    op.drop_constraint("fk_llm_usage_application", "llm_usage", type_="foreignkey")
    op.alter_column("llm_usage", "call_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_column("llm_usage", "application_id")
    op.drop_index("ix_scores_latest", table_name="scores")
    op.drop_column("jobs", "shortlist_rules")
