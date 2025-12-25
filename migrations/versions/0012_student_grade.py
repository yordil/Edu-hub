"""student grade (9-12), with the stream tied to it

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("grade", sa.SmallInteger(), nullable=True))

    # Existing students who chose a stream are grade 11 or 12; record them as 12. That is the one
    # choice that changes nothing for them: their stream keeps the same subjects, and a ceiling of
    # 12 hides no content. A backfill must not quietly take away access someone already had.
    # Students without a stream keep a NULL grade, which means "no ceiling" until they set one.
    op.execute("UPDATE users SET grade = 12 WHERE role = 'student' AND stream IS NOT NULL")

    op.create_check_constraint(op.f("ck_users_grade_range"), "users", "grade IS NULL OR grade BETWEEN 9 AND 12")
    op.create_check_constraint(op.f("ck_users_no_stream_before_11"), "users", "grade IS NULL OR grade >= 11 OR stream IS NULL")
    op.create_check_constraint(op.f("ck_users_stream_from_11"), "users", "grade IS NULL OR grade <= 10 OR stream IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_stream_from_11"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_no_stream_before_11"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_grade_range"), "users", type_="check")
    op.drop_column("users", "grade")
