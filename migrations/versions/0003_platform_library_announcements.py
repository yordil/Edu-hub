"""platform admin, teacher invite codes, shared question library, announcements

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_platform_admin", sa.Boolean(), server_default="false", nullable=False))

    op.add_column("organizations", sa.Column("is_internal", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("organizations", sa.Column("teacher_code", sa.String(16), nullable=True))
    op.execute("UPDATE organizations SET teacher_code = upper(substr(md5(random()::text || id::text), 1, 8))")
    op.alter_column("organizations", "teacher_code", nullable=False)
    op.create_unique_constraint("uq_organizations_teacher_code", "organizations", ["teacher_code"])

    op.alter_column("questions", "org_id", nullable=True)
    op.add_column("questions", sa.Column("grade", sa.SmallInteger(), nullable=True))
    op.add_column("questions", sa.Column("source", sa.String(200), nullable=True))

    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("author_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_announcements_org_id", "announcements", ["org_id"])


def downgrade() -> None:
    op.drop_table("announcements")
    op.execute("DELETE FROM questions WHERE org_id IS NULL")
    op.drop_column("questions", "source")
    op.drop_column("questions", "grade")
    op.alter_column("questions", "org_id", nullable=False)
    op.drop_constraint("uq_organizations_teacher_code", "organizations", type_="unique")
    op.drop_column("organizations", "teacher_code")
    op.drop_column("organizations", "is_internal")
    op.drop_column("users", "is_platform_admin")
