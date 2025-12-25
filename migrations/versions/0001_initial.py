"""initial schema: organizations, users, question bank, attempts

Revision ID: 0001
Revises:
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Needed later for embedding search on the question bank.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("join_code", sa.String(16), nullable=False, unique=True),
        sa.Column("created_at", ts, nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_org_id", "users", ["org_id"])

    op.create_table(
        "questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(80), nullable=False),
        sa.Column("topic", sa.String(120), nullable=False),
        sa.Column("difficulty", sa.String(16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_questions_org_subject_topic", "questions", ["org_id", "subject", "topic"])

    op.create_table(
        "choices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_choices_question_id", "choices", ["question_id"])

    op.create_table(
        "attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(80), nullable=True),
        sa.Column("topic", sa.String(120), nullable=True),
        sa.Column("difficulty", sa.String(16), nullable=True),
        sa.Column("started_at", ts, nullable=False),
        sa.Column("submitted_at", ts, nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
    )
    op.create_index("ix_attempts_user_id", "attempts", ["user_id"])

    op.create_table(
        "attempt_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("selected_choice_id", sa.Uuid(), sa.ForeignKey("choices.id"), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.UniqueConstraint("attempt_id", "question_id", name="uq_attempt_items_attempt_question"),
    )
    op.create_index("ix_attempt_items_attempt_id", "attempt_items", ["attempt_id"])
    op.create_index("ix_attempt_items_question_id", "attempt_items", ["question_id"])


def downgrade() -> None:
    op.drop_table("attempt_items")
    op.drop_table("attempts")
    op.drop_table("choices")
    op.drop_table("questions")
    op.drop_table("users")
    op.drop_table("organizations")
