"""tenant assistant conversations, messages and pending actions

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-26

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("title", sa.String, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "org_id"),
    )
    op.create_index("ix_assistant_conversations_org_id", "assistant_conversations", ["org_id"])
    op.create_index("ix_assistant_conversations_user_id", "assistant_conversations", ["user_id"])

    op.create_table(
        "assistant_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String, nullable=True),
        sa.Column("name", sa.String, nullable=True),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fairness_flags", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "org_id"],
            ["assistant_conversations.id", "assistant_conversations.org_id"],
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')", name="ck_assistant_messages_role"
        ),
    )
    op.create_index("ix_assistant_messages_org_id", "assistant_messages", ["org_id"])
    op.create_index(
        "ix_assistant_messages_conversation",
        "assistant_messages",
        ["conversation_id", "created_at"],
    )

    op.create_table(
        "assistant_pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("id", "org_id"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "org_id"],
            ["assistant_conversations.id", "assistant_conversations.org_id"],
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired')",
            name="ck_assistant_pending_actions_status",
        ),
        sa.CheckConstraint(
            "action IN ('start_screening')", name="ck_assistant_pending_actions_action"
        ),
    )
    op.create_index(
        "ix_assistant_pending_actions_org_id", "assistant_pending_actions", ["org_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_pending_actions_org_id", table_name="assistant_pending_actions")
    op.drop_table("assistant_pending_actions")
    op.drop_index("ix_assistant_messages_conversation", table_name="assistant_messages")
    op.drop_index("ix_assistant_messages_org_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")
    op.drop_index("ix_assistant_conversations_user_id", table_name="assistant_conversations")
    op.drop_index("ix_assistant_conversations_org_id", table_name="assistant_conversations")
    op.drop_table("assistant_conversations")
