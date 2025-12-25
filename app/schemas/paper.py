import uuid
from datetime import datetime
from typing import Literal

from pydantic import AfterValidator, BaseModel, Field
from typing_extensions import Annotated

from app.core.curriculum import validate_subject
from app.models.quiz import Difficulty

Subject = Annotated[str, AfterValidator(validate_subject)]


class PaperUpload(BaseModel):
    """Form fields sent with the PDF; anything left empty is read from the paper's header."""

    title: str | None = Field(default=None, max_length=200)
    subject: Subject | None = None
    grade: int | None = Field(default=None, ge=1, le=12)
    year: str | None = Field(default=None, max_length=20)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=240)
    default_topic: str | None = Field(default=None, max_length=120)
    library: bool = False


class PaperPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    subject: Subject | None = None
    grade: int | None = Field(default=None, ge=1, le=12)
    year: str | None = Field(default=None, max_length=20)
    time_limit_minutes: int | None = Field(default=None, ge=1, le=240)
    default_topic: str | None = Field(default=None, min_length=1, max_length=120)


class ItemUpdate(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=5000)
    choices: list[Annotated[str, Field(min_length=1, max_length=500)]] | None = Field(default=None, min_length=2, max_length=6)
    answer: int | None = Field(default=None, ge=0, le=5)
    topic: str | None = Field(default=None, max_length=120)
    difficulty: Difficulty | None = None
    explanation: str | None = Field(default=None, max_length=5000)
    include: bool | None = None


class PaperItemOut(BaseModel):
    number: int
    prompt: str
    choices: list[str]
    answer: int | None
    topic: str | None
    difficulty: str | None = None
    explanation: str | None = None
    include: bool
    issues: list[str]
    problems: list[str]
    page: int


PaperStatus = Literal["queued", "processing", "review", "published", "failed"]


class PaperSummary(BaseModel):
    id: uuid.UUID
    scope: Literal["school", "library"]
    status: PaperStatus
    title: str
    subject: str | None
    grade: int | None
    year: str | None
    time_limit_minutes: int | None
    questions: int
    created_at: datetime
    published_at: datetime | None


class PaperDetail(PaperSummary):
    filename: str
    default_topic: str
    page_count: int
    scanned: bool
    ocr_used: bool
    has_key: bool
    warnings: list[str]
    ready: int
    items: list[PaperItemOut]
    # Set when status is "failed": why the parse could not finish.
    failure_reason: str | None = None


class PublishResult(BaseModel):
    paper: PaperSummary
    created: int
    skipped: list[int]


class Explanation(BaseModel):
    question_id: uuid.UUID
    source: Literal["teacher", "ai"]
    explanation: str
    official_choice_id: uuid.UUID
    # AI review of the answer key; "disputed" means the AI is confident another choice is right.
    ai_check: str | None
    ai_suggested_choice_id: uuid.UUID | None


class AnswerReview(BaseModel):
    action: Literal["keep", "accept_ai"]
