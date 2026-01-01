import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    updated_at: datetime


class TutorChatIn(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be blank")
        return v


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int


class TutorChatOut(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    tools_used: list[str]
    usage: TokenUsage


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    updated_at: datetime


class TutorMessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    tools_used: list[str]
    created_at: datetime


class ConversationDetail(BaseModel):
    id: uuid.UUID
    title: str
    messages: list[TutorMessageOut]


class FeatureUsage(BaseModel):
    feature: str
    calls: int
    tokens: int


class SchoolAIUsage(BaseModel):
    period_start: datetime
    tokens_used: int
    budget: int
    remaining: int
    calls: int
    by_feature: list[FeatureUsage]


class TutorAccess(BaseModel):
    """`plan` is "pro" for a paying school, otherwise "trial": a one-time token allowance shared by
    the whole school. `allowed` says whether the next message will be accepted."""

    plan: Literal["pro", "trial"]
    allowed: bool
    trial_allowance: int
    trial_used: int
    trial_remaining: int
