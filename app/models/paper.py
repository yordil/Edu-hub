import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import Timestamped, UUIDPk


class ExamPaper(UUIDPk, Timestamped, Base):
    """An uploaded past paper: parsed items wait in `items` for staff review, then publish into the question bank
    and the paper becomes takeable as a timed exam (question order kept in `question_ids`)."""

    __tablename__ = "exam_papers"
    __table_args__ = (
        # Workers claim from this table. Published papers pile up forever but are never claimed,
        # so the index covers only waiting and in-flight rows: claiming stays cheap as history grows.
        Index("ix_exam_papers_claimable", "created_at", postgresql_where=text("status IN ('queued', 'processing')")),
    )

    # NULL = EduHub library paper (uploaded by a platform admin), visible to every school.
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # queued -> processing -> review -> published, or failed. The row is the source of truth:
    # the queue only hints that work is waiting, so a lost message cannot lose the upload.
    status: Mapped[str] = mapped_column(String(16), default="queued")
    filename: Mapped[str] = mapped_column(String(255))
    # Where the original upload lives, so a failed parse retries against the same bytes.
    storage_key: Mapped[str | None] = mapped_column(String(500))
    key_storage_key: Mapped[str | None] = mapped_column(String(500))
    pasted_key: Mapped[str | None] = mapped_column(Text)
    # The form fields as submitted; applied over the parsed header at process time.
    upload_meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    processing_started_at: Mapped[datetime | None] = mapped_column()
    title: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(80))
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    year: Mapped[str | None] = mapped_column(String(20))
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer)
    default_topic: Mapped[str] = mapped_column(String(120), default="general")
    page_count: Mapped[int] = mapped_column(Integer)
    scanned: Mapped[bool] = mapped_column(default=False)
    ocr_used: Mapped[bool] = mapped_column(default=False)
    has_key: Mapped[bool] = mapped_column(default=False)
    # [{number, prompt, choices[], answer (index|None), topic, include, issues[], page}]
    items: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)
    question_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    published_at: Mapped[datetime | None] = mapped_column()
