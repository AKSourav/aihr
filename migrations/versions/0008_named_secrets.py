"""named reusable secrets replacing one credential per provider

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-22

"""
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

DEFAULT_PROVIDER = {"stt": "deepgram", "tts": "cartesia", "llm_call": "openai", "llm_summary": "openai"}


def upgrade() -> None:
    op.add_column("accounts", sa.Column("provider_config", sa.JSON(), nullable=True))
    op.create_table(
        "account_owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("account_id", "user_id"),
    )
    op.create_index("ix_account_owners_account_id", "account_owners", ["account_id"])
    op.create_index("ix_account_owners_user_id", "account_owners", ["user_id"])
    op.execute("""
        INSERT INTO account_owners (id, account_id, user_id)
        SELECT DISTINCT gen_random_uuid(), o.account_id, m.user_id
        FROM memberships m
        JOIN organizations o ON o.id = m.org_id
        WHERE m.role = 'owner'
        ON CONFLICT (account_id, user_id) DO NOTHING
    """)
    op.create_table(
        "secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("last4", sa.String, nullable=False),
        sa.Column("key_version", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("account_id", "name"),
    )
    op.create_index("ix_secrets_account_id", "secrets", ["account_id"])

    connection = op.get_bind()
    credentials = connection.execute(
        sa.text(
            "SELECT c.org_id, o.account_id, c.job_id, c.provider, c.base_url,"
            " c.secret_ciphertext, c.secret_last4, c.key_version"
            " FROM provider_credentials c JOIN organizations o ON o.id = c.org_id"
            " ORDER BY c.job_id NULLS FIRST, c.provider"
        )
    ).mappings().all()

    secret_for = {}
    by_value = {}
    for row in credentials:
        account_id = row["account_id"]
        existing = by_value.get((account_id, row["secret_ciphertext"]))
        if existing is None:
            name = row["provider"]
            suffix = 1
            while connection.execute(
                sa.text("SELECT 1 FROM secrets WHERE account_id = :a AND name = :n"),
                {"a": account_id, "n": name},
            ).scalar():
                suffix += 1
                name = f"{row['provider']} {suffix}"
            existing = connection.execute(
                sa.text(
                    "INSERT INTO secrets (id, account_id, name, ciphertext, last4, key_version)"
                    " VALUES (gen_random_uuid(), :account_id, :name, :ciphertext, :last4,"
                    " :key_version) RETURNING id"
                ),
                {
                    "account_id": account_id,
                    "name": name,
                    "ciphertext": row["secret_ciphertext"],
                    "last4": row["secret_last4"],
                    "key_version": row["key_version"],
                },
            ).scalar()
            by_value[(account_id, row["secret_ciphertext"])] = existing
        secret_for[(row["org_id"], row["job_id"], row["provider"])] = (existing, row["base_url"])

    for table, owner in (("organizations", None), ("jobs", "job")):
        rows = connection.execute(
            sa.text(f"SELECT id, org_id, provider_config FROM {table} WHERE provider_config IS NOT NULL")
            if owner
            else sa.text(f"SELECT id, provider_config FROM {table} WHERE provider_config IS NOT NULL")
        ).mappings().all()

        for row in rows:
            old = row["provider_config"] or {}
            if isinstance(old, str):
                old = json.loads(old)
            org_id = row["org_id"] if owner else row["id"]
            job_id = row["id"] if owner else None
            new = _rewrite(old, secret_for, org_id, job_id)
            connection.execute(
                sa.text(f"UPDATE {table} SET provider_config = :cfg WHERE id = :id"),
                {"cfg": json.dumps(new), "id": row["id"]},
            )

    op.drop_table("provider_credentials")


def _rewrite(old: dict, secret_for: dict, org_id, job_id) -> dict:
    def slot(prefix: str, provider_key: str, value_key: str, voice: bool = False) -> dict:
        explicit = old.get(provider_key)
        provider = explicit or DEFAULT_PROVIDER[prefix]
        entry = {}
        found = secret_for.get((org_id, job_id, provider)) or secret_for.get(
            (org_id, None, provider)
        )
        if explicit or found:
            entry["provider"] = provider
        if found:
            entry["secret_id"] = str(found[0])
            if found[1]:
                entry["base_url"] = found[1]
        value = old.get(value_key)
        if value:
            entry["voice_id" if voice else "model"] = value
        if old.get("llm_base_url") and prefix.startswith("llm"):
            entry["base_url"] = old["llm_base_url"]
        return entry

    return {
        "stt": slot("stt", "stt_provider", "stt_model"),
        "tts": slot("tts", "tts_provider", "tts_voice_id", voice=True),
        "llm_call": slot("llm_call", "llm_provider", "llm_call_model"),
        "llm_summary": slot("llm_summary", "llm_provider", "llm_summary_model"),
    }


def downgrade() -> None:
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
    )
    op.create_index("ix_provider_credentials_org_id", "provider_credentials", ["org_id"])
    op.drop_index("ix_account_owners_user_id", table_name="account_owners")
    op.drop_index("ix_account_owners_account_id", table_name="account_owners")
    op.drop_table("account_owners")
    op.drop_column("accounts", "provider_config")
    op.drop_index("ix_secrets_account_id", table_name="secrets")
    op.drop_table("secrets")
