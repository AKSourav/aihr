"""per-job provider selection and encrypted tenant provider credentials

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("provider_config", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("provider_config", sa.JSON(), nullable=True))

    op.create_table(
        "provider_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String, nullable=False),
        sa.Column("base_url", sa.String, nullable=True),
        sa.Column("secret_ciphertext", sa.Text, nullable=False),
        sa.Column("secret_last4", sa.String, nullable=False),
        sa.Column("key_version", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id", "org_id"], ["jobs.id", "jobs.org_id"]),
        sa.CheckConstraint(
            "provider IN ('openai', 'deepgram', 'cartesia', 'elevenlabs')",
            name="ck_provider_credentials_provider",
        ),
    )
    op.create_index("ix_provider_credentials_org_id", "provider_credentials", ["org_id"])
    op.create_index(
        "uq_provider_credentials_org",
        "provider_credentials",
        ["org_id", "provider"],
        unique=True,
        postgresql_where=sa.text("job_id IS NULL"),
    )
    op.create_index(
        "uq_provider_credentials_job",
        "provider_credentials",
        ["job_id", "provider"],
        unique=True,
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_provider_credentials_job", table_name="provider_credentials")
    op.drop_index("uq_provider_credentials_org", table_name="provider_credentials")
    op.drop_index("ix_provider_credentials_org_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
    op.drop_column("jobs", "provider_config")
    op.drop_column("organizations", "provider_config")
