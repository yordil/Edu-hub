"""async past-paper ingestion (storage-backed queue) and an audit log

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("exam_papers", sa.Column("storage_key", sa.String(500), nullable=True))
    op.add_column("exam_papers", sa.Column("key_storage_key", sa.String(500), nullable=True))
    op.add_column("exam_papers", sa.Column("pasted_key", sa.Text(), nullable=True))
    op.add_column(
        "exam_papers",
        sa.Column("upload_meta", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column("exam_papers", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column("exam_papers", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("exam_papers", sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True))
    # Papers that already exist were parsed inline and are past the queue entirely.
    op.alter_column("exam_papers", "status", server_default="queued")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity", sa.String(40), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("details", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_org_created", "audit_log", ["org_id", "created_at"])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.alter_column("exam_papers", "status", server_default="review")
    for col in (
        "processing_started_at",
        "attempts",
        "failure_reason",
        "upload_meta",
        "pasted_key",
        "key_storage_key",
        "storage_key",
    ):
        op.drop_column("exam_papers", col)
