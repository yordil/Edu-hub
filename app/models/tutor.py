import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import Timestamped, UUIDPk, utcnow


class TutorConversation(UUIDPk, Timestamped, Base):
    __tablename__ = "tutor_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    messages: Mapped[list["TutorMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="TutorMessage.created_at", lazy="selectin"
    )


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class TutorMessage(UUIDPk, Timestamped, Base):
    __tablename__ = "tutor_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tutor_conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, native_enum=False, length=16))
    content: Mapped[str] = mapped_column(Text)
    # Tools the agent used to produce this reply, comma-separated (assistant messages only).
    tools_used: Mapped[str | None] = mapped_column(String(300))

    conversation: Mapped[TutorConversation] = relationship(back_populates="messages", lazy="raise")


class AIUsage(UUIDPk, Timestamped, Base):
    """One row per model call: the ledger behind per-school budgets and billing."""

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_org_created", "org_id", "created_at"),)

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    feature: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # "ok" or "error"; failed provider calls are logged too so error rates are measurable.
    outcome: Mapped[str] = mapped_column(String(16), default="ok", server_default="ok")


class GuardrailEvent(UUIDPk, Timestamped, Base):
    """A tutor message that a guardrail blocked or modified, for safety monitoring."""

    __tablename__ = "ai_guardrail_events"
    __table_args__ = (Index("ix_ai_guardrail_events_created", "created_at"),)

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(16))


class TutorMemory(UUIDPk, Timestamped, Base):
    """A durable fact the tutor saved about a student (goal, preference, struggle), recalled in every chat."""

    __tablename__ = "tutor_memories"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
