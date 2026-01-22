import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=5000)
    pinned: bool = False


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    scope: Literal["school", "platform"]
    title: str
    body: str
    pinned: bool
    author_id: uuid.UUID | None
    author_name: str | None
    created_at: datetime


class RoleCounts(BaseModel):
    students: int
    teachers: int
    admins: int


class PlatformTotals(BaseModel):
    schools: int
    users: RoleCounts
    library_questions: int
    school_questions: int
    attempts_submitted: int
    answers_graded: int
    accuracy: float | None
    ai_tokens_30d: int


class Activity(BaseModel):
    new_schools_30d: int
    new_students_30d: int
    active_students_7d: int
    active_students_30d: int
    attempts_7d: int
    attempts_30d: int


class DailyPoint(BaseModel):
    day: date
    attempts: int
    signups: int


class SchoolRow(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    students: int
    teachers: int
    own_questions: int
    attempts: int
    active_students_30d: int
    ai_tokens_30d: int
    last_activity: datetime | None


class SubjectRow(BaseModel):
    subject: str
    answered: int
    accuracy: float


class PlatformMetrics(BaseModel):
    generated_at: datetime
    totals: PlatformTotals
    activity: Activity
    daily: list[DailyPoint]
    schools: list[SchoolRow]
    subjects: list[SubjectRow]


class AIDay(BaseModel):
    day: date
    calls: int
    errors: int
    tokens: int
    p95_ms: int | None


class AIModelRow(BaseModel):
    model: str
    calls: int
    tokens: int
    p50_ms: int | None


class GuardrailRow(BaseModel):
    category: str
    stage: str
    count: int


class AIMetrics(BaseModel):
    days: int
    calls: int
    errors: int
    error_rate: float | None
    input_tokens: int
    output_tokens: int
    p50_ms: int | None
    p95_ms: int | None
    conversations: int
    daily: list[AIDay]
    models: list[AIModelRow]
    guardrails: list[GuardrailRow]
