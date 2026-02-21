"""AI tutor: conversations, messages and the per-school token usage ledger

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tutor_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
    )
    op.create_index("ix_tutor_conversations_user_id", "tutor_conversations", ["user_id"])

    op.create_table(
        "tutor_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("tutor_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tools_used", sa.String(300), nullable=True),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_tutor_messages_conversation_id", "tutor_messages", ["conversation_id"])

    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("feature", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_ai_usage_org_created", "ai_usage", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_table("ai_usage")
    op.drop_table("tutor_messages")
    op.drop_table("tutor_conversations")
