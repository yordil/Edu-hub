"""partial index for claiming papers from the ingestion queue

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The worker's claim query filters on these statuses and orders by created_at. Restricting the
    # index to them keeps it as small as the queue, however many published papers accumulate.
    op.create_index(
        "ix_exam_papers_claimable",
        "exam_papers",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_exam_papers_claimable", table_name="exam_papers")
