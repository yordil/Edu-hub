"""past-paper PDF imports and AI answer-key checks

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "exam_papers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(80), nullable=True),
        sa.Column("grade", sa.SmallInteger(), nullable=True),
        sa.Column("year", sa.String(20), nullable=True),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("default_topic", sa.String(120), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("scanned", sa.Boolean(), nullable=False),
        sa.Column("ocr_used", sa.Boolean(), nullable=False),
        sa.Column("has_key", sa.Boolean(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("question_ids", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", ts, nullable=True),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_exam_papers_org_id", "exam_papers", ["org_id"])

    op.add_column("questions", sa.Column("ai_explanation", sa.Text(), nullable=True))
    op.add_column("questions", sa.Column("ai_check", sa.String(16), nullable=True))
    op.add_column("questions", sa.Column("ai_suggested_choice_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_questions_ai_suggested_choice_id_choices", "questions", "choices", ["ai_suggested_choice_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_questions_ai_suggested_choice_id_choices", "questions", type_="foreignkey")
    op.drop_column("questions", "ai_suggested_choice_id")
    op.drop_column("questions", "ai_check")
    op.drop_column("questions", "ai_explanation")
    op.drop_table("exam_papers")
