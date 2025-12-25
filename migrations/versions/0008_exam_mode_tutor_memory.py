"""exam simulation (attempt deadline, flagged items) and long-term tutor memory

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("attempts", sa.Column("deadline", ts, nullable=True))
    op.add_column("attempt_items", sa.Column("flagged", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    op.create_table(
        "tutor_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.String(300), nullable=False),
        sa.Column("created_at", ts, nullable=False),
        sa.Column("updated_at", ts, nullable=False),
    )
    op.create_index("ix_tutor_memories_user_id", "tutor_memories", ["user_id"])


def downgrade() -> None:
    op.drop_table("tutor_memories")
    op.drop_column("attempt_items", "flagged")
    op.drop_column("attempts", "deadline")
