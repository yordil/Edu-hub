import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.curriculum import normalize_subject, normalize_tag, validate_subject
from app.models.quiz import AttemptMode, Difficulty

__all__ = ["normalize_tag"]

# Authoring must use a curriculum subject; filters only normalize (unknown subjects simply match nothing).
Subject = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(validate_subject)]
SubjectFilter = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(normalize_subject)]
Topic = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(normalize_tag)]
Grade = Annotated[int, Field(ge=1, le=12)]


class ChoiceIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    is_correct: bool = False


class QuestionCreate(BaseModel):
    subject: Subject
    topic: Topic
    difficulty: Difficulty
    grade: Grade | None = None
    source: str | None = Field(default=None, max_length=200)
    prompt: str = Field(min_length=1, max_length=5000)
    explanation: str | None = Field(default=None, max_length=5000)
    choices: list[ChoiceIn] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _check_choices(self) -> "QuestionCreate":
        if sum(c.is_correct for c in self.choices) != 1:
            raise ValueError("exactly one choice must be correct")
        texts = [normalize_tag(c.text) for c in self.choices]
        if len(set(texts)) != len(texts):
            raise ValueError("choices must be unique")
        return self


class QuestionImport(BaseModel):
    questions: list[QuestionCreate] = Field(min_length=1, max_length=500)


class ImportResult(BaseModel):
    created: int
    possible_duplicates: int = 0


class ChoicePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str


class ChoiceAuthor(ChoicePublic):
    is_correct: bool


class QuestionPublic(BaseModel):
    """Student-facing view: never contains the answer."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject: str
    topic: str
    difficulty: Difficulty
    grade: int | None
    source: str | None
    is_library: bool
    prompt: str
    choices: list[ChoicePublic]


class QuestionAuthor(QuestionPublic):
    explanation: str | None
    choices: list[ChoiceAuthor]
    created_by_id: uuid.UUID | None
    created_at: datetime
    rating: float
    rating_count: int
    ai_check: str | None = None


class DisputedQuestion(BaseModel):
    question: QuestionAuthor
    ai_suggested_choice_id: uuid.UUID | None
    ai_explanation: str | None


class SearchHit(BaseModel):
    question: QuestionPublic
    similarity: float


class DuplicateCheck(BaseModel):
    text: str = Field(min_length=5, max_length=5000)


class AttemptCreate(BaseModel):
    mode: AttemptMode = AttemptMode.random
    subject: SubjectFilter | None = None
    topic: Topic | None = None
    difficulty: Difficulty | None = None
    grade: Grade | None = None
    size: int = Field(default=10, ge=1, le=100)
    # Exam mode only; defaults to 1.5 minutes per question.
    time_limit_minutes: int | None = Field(default=None, ge=1, le=240)

    @model_validator(mode="after")
    def _check_mode(self) -> "AttemptCreate":
        if self.mode is not AttemptMode.exam:
            if self.size > 50:
                raise ValueError("practice sessions have at most 50 questions; use exam mode for up to 100")
            if self.time_limit_minutes is not None:
                raise ValueError("time_limit_minutes applies to exam mode only")
        return self


class AnswerIn(BaseModel):
    question_id: uuid.UUID
    choice_id: uuid.UUID


class AttemptSubmit(BaseModel):
    answers: list[AnswerIn] = Field(max_length=100)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "AttemptSubmit":
        ids = [a.question_id for a in self.answers]
        if len(set(ids)) != len(ids):
            raise ValueError("each question may be answered only once")
        return self


class ProgressItem(BaseModel):
    question_id: uuid.UUID
    choice_id: uuid.UUID | None = None
    flagged: bool = False


class AttemptProgress(BaseModel):
    """Autosave: the full current state of the answer sheet."""

    items: list[ProgressItem] = Field(max_length=100)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "AttemptProgress":
        ids = [i.question_id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("each question may appear only once")
        return self


class ItemResult(BaseModel):
    question: QuestionPublic
    selected_choice_id: uuid.UUID | None
    flagged: bool = False
    predicted_success: float | None = None
    # Revealed only after submission.
    correct_choice_id: uuid.UUID | None = None
    is_correct: bool | None = None
    explanation: str | None = None
    ai_check: str | None = None


class AttemptSummary(BaseModel):
    id: uuid.UUID
    mode: AttemptMode
    subject: str | None
    topic: str | None
    difficulty: Difficulty | None
    started_at: datetime
    deadline: datetime | None = None
    submitted_at: datetime | None
    score: int | None
    total: int


class AttemptDetail(AttemptSummary):
    items: list[ItemResult]


class TopicStat(BaseModel):
    subject: str
    topic: str
    answered: int
    correct: int
    accuracy: float


class MasteryOut(BaseModel):
    subject: str
    topic: str
    mastery: float
    level: str
    answered: int
    correct: int
    updated_at: datetime


class NextQuestion(BaseModel):
    question: QuestionPublic
    predicted_success: float
    mastery: float
    reason: str


class ActivityDay(BaseModel):
    date: date
    sessions: int
    questions: int
    correct: int


class Activity(BaseModel):
    days: list[ActivityDay]
    current_streak: int
    longest_streak: int
    active_days: int
    total_questions: int
