import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import Timestamped, UUIDPk, utcnow


class Difficulty(str, enum.Enum):
    easy = "easy"
    medium = "medium"
    hard = "hard"


class AttemptMode(str, enum.Enum):
    random = "random"
    adaptive = "adaptive"
    exam = "exam"


_difficulty = Enum(Difficulty, native_enum=False, length=16)


class Question(UUIDPk, Timestamped, Base):
    __tablename__ = "questions"
    __table_args__ = (Index("ix_questions_org_subject_topic", "org_id", "subject", "topic"),)

    # NULL = shared library question, visible to every school.
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(80))
    topic: Mapped[str] = mapped_column(String(120))
    difficulty: Mapped[Difficulty] = mapped_column(_difficulty)
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    source: Mapped[str | None] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Learned difficulty (Elo beta); seeded from `difficulty`, then calibrated by student answers.
    rating: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    rating_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Generated on first request ("Explain with AI") and cached; teacher-written `explanation` wins when present.
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    # AI review of the answer key: "agrees" | "unsure" | "disputed" | "confirmed" (staff kept the key) | "corrected".
    ai_check: Mapped[str | None] = mapped_column(String(16))
    ai_suggested_choice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("choices.id", ondelete="SET NULL", use_alter=True))

    choices: Mapped[list["Choice"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="Choice.position",
        lazy="selectin",
        # `ai_suggested_choice_id` is a second questions->choices path, so name the one that owns this side.
        foreign_keys="Choice.question_id",
    )

    @property
    def is_library(self) -> bool:
        return self.org_id is None

    @property
    def correct_choice(self) -> "Choice":
        return next(c for c in self.choices if c.is_correct)


class Choice(UUIDPk, Base):
    __tablename__ = "choices"

    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(String(500))
    is_correct: Mapped[bool] = mapped_column(default=False)

    question: Mapped[Question] = relationship(back_populates="choices", lazy="raise", foreign_keys=question_id)


class Attempt(UUIDPk, Base):
    __tablename__ = "attempts"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str | None] = mapped_column(String(80))
    topic: Mapped[str | None] = mapped_column(String(120))
    difficulty: Mapped[Difficulty | None] = mapped_column(_difficulty)
    mode: Mapped[AttemptMode] = mapped_column(
        Enum(AttemptMode, native_enum=False, length=16),
        default=AttemptMode.random,
        server_default=AttemptMode.random.value,
    )
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Exam mode only: after this (plus a short grace) the server grades whatever was saved.
    deadline: Mapped[datetime | None] = mapped_column()
    submitted_at: Mapped[datetime | None] = mapped_column()
    score: Mapped[int | None] = mapped_column(Integer)

    items: Mapped[list["AttemptItem"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="AttemptItem.position",
        lazy="selectin",
    )

    @property
    def total(self) -> int:
        return len(self.items)


class AttemptItem(UUIDPk, Base):
    """One question served in an attempt, and what the student picked for it."""

    __tablename__ = "attempt_items"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id", name="uq_attempt_items_attempt_question"),)

    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("questions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    selected_choice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("choices.id"))
    is_correct: Mapped[bool | None] = mapped_column()
    flagged: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # Model's P(correct) when served; kept so prediction quality can be measured later.
    predicted_p: Mapped[float | None] = mapped_column(Float)

    attempt: Mapped[Attempt] = relationship(back_populates="items", lazy="raise")
    question: Mapped[Question] = relationship(lazy="selectin")


class TopicMastery(UUIDPk, Base):
    """A student's Elo skill (theta) in one subject/topic."""

    __tablename__ = "topic_mastery"
    __table_args__ = (
        UniqueConstraint("user_id", "subject", "topic", name="uq_topic_mastery_user_subject_topic"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    subject: Mapped[str] = mapped_column(String(80))
    topic: Mapped[str] = mapped_column(String(120))
    theta: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))
    answered: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    correct: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
