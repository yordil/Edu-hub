import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.curriculum import Stream
from app.core.db import Base
from app.models.base import Timestamped, UUIDPk
from app.models.quiz import Question
from app.models.user import User


class Contest(UUIDPk, Timestamped, Base):
    """A timed, fixed set of questions everyone answers in the same window, ranked on a leaderboard."""

    __tablename__ = "contests"
    __table_args__ = (Index("ix_contests_starts_at", "starts_at"),)

    # NULL = EduHub-wide contest open to every school.
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(80))
    stream: Mapped[Stream | None] = mapped_column(Enum(Stream, native_enum=False, length=16))
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    starts_at: Mapped[datetime] = mapped_column()
    ends_at: Mapped[datetime] = mapped_column()
    # Personal time limit once a participant starts (always cut off at ends_at).
    duration_minutes: Mapped[int] = mapped_column(Integer)

    questions: Mapped[list["ContestQuestion"]] = relationship(
        cascade="all, delete-orphan", order_by="ContestQuestion.position", lazy="selectin"
    )


class ContestQuestion(UUIDPk, Base):
    __tablename__ = "contest_questions"
    __table_args__ = (UniqueConstraint("contest_id", "question_id", name="uq_contest_questions_contest_question"),)

    contest_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"))
    position: Mapped[int] = mapped_column(Integer)

    question: Mapped[Question] = relationship(lazy="selectin")


class ContestEntry(UUIDPk, Base):
    __tablename__ = "contest_entries"
    __table_args__ = (UniqueConstraint("contest_id", "user_id", name="uq_contest_entries_contest_user"),)

    contest_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column()
    submitted_at: Mapped[datetime | None] = mapped_column()
    score: Mapped[int | None] = mapped_column(Integer)
    time_taken_seconds: Mapped[int | None] = mapped_column(Integer)

    user: Mapped[User] = relationship(lazy="joined")
    answers: Mapped[list["ContestAnswer"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class ContestAnswer(UUIDPk, Base):
    __tablename__ = "contest_answers"

    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contest_entries.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"))
    choice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("choices.id"))
    is_correct: Mapped[bool] = mapped_column()
