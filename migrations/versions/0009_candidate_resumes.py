"""candidate resumes and profile links

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-26

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("links", sa.JSON(), nullable=True))

    op.create_table(
        "resumes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("pdf_url", sa.String, nullable=False),
        sa.Column("filename", sa.String, nullable=False),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("model_version", sa.String, nullable=True),
        sa.Column("prompt_version", sa.String, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "org_id"),
        sa.ForeignKeyConstraint(
            ["candidate_id", "org_id"], ["candidates.id", "candidates.org_id"]
        ),
        sa.ForeignKeyConstraint(["job_id", "org_id"], ["jobs.id", "jobs.org_id"]),
        sa.CheckConstraint(
            "status IN ('extracting', 'ready', 'failed')", name="ck_resumes_status"
        ),
    )
    op.create_index("ix_resumes_org_id", "resumes", ["org_id"])
    op.create_index("ix_resumes_candidate_id", "resumes", ["candidate_id", "created_at"])
    op.create_index("ix_resumes_job_id", "resumes", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_resumes_job_id", table_name="resumes")
    op.drop_index("ix_resumes_candidate_id", table_name="resumes")
    op.drop_index("ix_resumes_org_id", table_name="resumes")
    op.drop_table("resumes")
    op.drop_column("candidates", "links")
