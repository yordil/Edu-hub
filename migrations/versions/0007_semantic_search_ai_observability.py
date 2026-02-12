"""semantic search (pgvector question embeddings), AI call latency/outcome, guardrail events

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ts = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "question_embeddings",
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.Column("updated_at", ts, nullable=False),
    )
    op.create_index(
        "ix_question_embeddings_hnsw",
        "question_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.add_column("ai_usage", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.add_column("ai_usage", sa.Column("outcome", sa.String(16), server_default="ok", nullable=False))

    op.create_table(
        "ai_guardrail_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", ts, nullable=False),
    )
    op.create_index("ix_ai_guardrail_events_created", "ai_guardrail_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("ai_guardrail_events")
    op.drop_column("ai_usage", "outcome")
    op.drop_column("ai_usage", "latency_ms")
    op.drop_table("question_embeddings")
