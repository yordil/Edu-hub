"""adaptive learning: question ratings, topic mastery, attempt mode

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("rating", sa.Float(), server_default=sa.text("0"), nullable=False))
    op.add_column("questions", sa.Column("rating_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.execute(
        "UPDATE questions SET rating = CASE difficulty WHEN 'easy' THEN -1.0 WHEN 'hard' THEN 1.0 ELSE 0.0 END"
    )

    op.add_column("attempts", sa.Column("mode", sa.String(16), server_default="random", nullable=False))
    op.add_column("attempt_items", sa.Column("predicted_p", sa.Float(), nullable=True))

    op.create_table(
        "topic_mastery",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(80), nullable=False),
        sa.Column("topic", sa.String(120), nullable=False),
        sa.Column("theta", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("answered", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("correct", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "subject", "topic", name="uq_topic_mastery_user_subject_topic"),
    )


def downgrade() -> None:
    op.drop_table("topic_mastery")
    op.drop_column("attempt_items", "predicted_p")
    op.drop_column("attempts", "mode")
    op.drop_column("questions", "rating_count")
    op.drop_column("questions", "rating")
