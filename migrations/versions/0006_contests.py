"""weekly contests: contests, fixed question sets, entries and graded answers

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "contests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(80), nullable=True),
        sa.Column("stream", sa.String(16), nullable=True),
        sa.Column("grade", sa.SmallInteger(), nullable=True),
        sa.Column("starts_at", ts, nullable=False),
        sa.Column("ends_at", ts, nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_contests_org_id", "contests", ["org_id"])
    op.create_index("ix_contests_starts_at", "contests", ["starts_at"])

    op.create_table(
        "contest_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("contest_id", sa.Uuid(), sa.ForeignKey("contests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("contest_id", "question_id", name="uq_contest_questions_contest_question"),
    )
    op.create_index("ix_contest_questions_contest_id", "contest_questions", ["contest_id"])

    op.create_table(
        "contest_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("contest_id", sa.Uuid(), sa.ForeignKey("contests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", ts, nullable=False),
        sa.Column("submitted_at", ts, nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("time_taken_seconds", sa.Integer(), nullable=True),
        sa.UniqueConstraint("contest_id", "user_id", name="uq_contest_entries_contest_user"),
    )
    op.create_index("ix_contest_entries_contest_id", "contest_entries", ["contest_id"])

    op.create_table(
        "contest_answers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("contest_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id"), nullable=False),
        sa.Column("choice_id", sa.Uuid(), sa.ForeignKey("choices.id"), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_contest_answers_entry_id", "contest_answers", ["entry_id"])


def downgrade() -> None:
    op.drop_table("contest_answers")
    op.drop_table("contest_entries")
    op.drop_table("contest_questions")
    op.drop_table("contests")
