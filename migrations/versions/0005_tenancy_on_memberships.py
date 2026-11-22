"""move tenancy from the user row onto memberships

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memberships_role", "memberships", type_="check")
    op.create_check_constraint(
        "ck_memberships_role",
        "memberships",
        "role IN ('owner', 'admin', 'recruiter', 'viewer')",
    )

    op.execute("""
        UPDATE memberships SET role = 'owner'
        FROM users, organizations
        WHERE memberships.user_id = users.id
          AND memberships.org_id = organizations.id
          AND users.account_role = 'owner'
          AND organizations.account_id = users.account_id
    """)
    op.execute("""
        INSERT INTO memberships (id, user_id, org_id, role, created_at)
        SELECT gen_random_uuid(), users.id, organizations.id, 'owner', now()
        FROM users
        JOIN organizations ON organizations.account_id = users.account_id
        WHERE users.account_role = 'owner'
          AND NOT EXISTS (
              SELECT 1 FROM memberships
              WHERE memberships.user_id = users.id AND memberships.org_id = organizations.id
          )
    """)

    op.drop_column("users", "account_role")
    op.drop_column("users", "account_id")
    op.drop_constraint("accounts_name_key", "accounts", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("accounts_name_key", "accounts", ["name"])
    op.add_column(
        "users", sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "users", sa.Column("account_role", sa.String(), nullable=False, server_default="member")
    )
    op.create_foreign_key("fk_users_account_id", "users", "accounts", ["account_id"], ["id"])

    op.execute("""
        UPDATE users SET account_role = 'owner', account_id = owned.account_id
        FROM (
            SELECT DISTINCT memberships.user_id, organizations.account_id
            FROM memberships
            JOIN organizations ON organizations.id = memberships.org_id
            WHERE memberships.role = 'owner'
        ) AS owned
        WHERE users.id = owned.user_id
    """)
    op.execute("UPDATE memberships SET role = 'admin' WHERE role = 'owner'")

    op.drop_constraint("ck_memberships_role", "memberships", type_="check")
    op.create_check_constraint(
        "ck_memberships_role",
        "memberships",
        "role IN ('admin', 'recruiter', 'viewer')",
    )
