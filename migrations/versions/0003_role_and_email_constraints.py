"""constrain membership roles and make user email unique

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18

"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

PLACEHOLDERS = """
    SELECT placeholder.id AS old_id, real.id AS new_id
    FROM users placeholder
    JOIN users real
      ON real.email = placeholder.email
     AND real.id <> placeholder.id
     AND real.keycloak_sub NOT LIKE 'seed:%'
    WHERE placeholder.keycloak_sub LIKE 'seed:%'
"""


def upgrade() -> None:
    op.execute(f"""
        DELETE FROM memberships placeholder
        USING ({PLACEHOLDERS}) AS collapse
        WHERE placeholder.user_id = collapse.old_id
          AND EXISTS (
              SELECT 1 FROM memberships kept
              WHERE kept.user_id = collapse.new_id AND kept.org_id = placeholder.org_id
          )
    """)
    for table, column in (
        ("memberships", "user_id"),
        ("break_glass_scopes", "user_id"),
        ("audit_events", "actor_user_id"),
    ):
        op.execute(f"""
            UPDATE {table} SET {column} = collapse.new_id
            FROM ({PLACEHOLDERS}) AS collapse
            WHERE {table}.{column} = collapse.old_id
        """)
    op.execute(f"DELETE FROM users WHERE id IN (SELECT old_id FROM ({PLACEHOLDERS}) AS collapse)")

    op.create_check_constraint(
        "ck_memberships_role",
        "memberships",
        "role IN ('admin', 'recruiter', 'viewer')",
    )
    op.create_index("uq_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_email", table_name="users")
    op.drop_constraint("ck_memberships_role", "memberships", type_="check")
