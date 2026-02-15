import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.curriculum import STREAM_SUBJECTS, Stream
from app.schemas.quiz import AnswerIn, Grade, ItemResult, QuestionPublic, Subject

ContestStatus = Literal["upcoming", "live", "ended"]


class ContestCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    subject: Subject | None = None
    stream: Stream | None = None
    grade: Grade | None = None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(default=15, ge=5, le=180)
    question_count: int = Field(default=10, ge=3, le=30)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    @model_validator(mode="after")
    def _check(self) -> "ContestCreate":
        if self.ends_at <= datetime.now(timezone.utc):
            raise ValueError("ends_at must be in the future")
        if (self.ends_at - self.starts_at).total_seconds() < self.duration_minutes * 60:
            raise ValueError("the contest window must be at least as long as the time limit")
        if self.stream and self.subject and self.subject not in STREAM_SUBJECTS[self.stream]:
            raise ValueError(f"{self.subject} is not part of the {self.stream.value} stream")
        return self


class EntryOut(BaseModel):
    started_at: datetime
    deadline: datetime
    submitted_at: datetime | None
    score: int | None
    total: int
    time_taken_seconds: int | None
    rank: int | None


class ContestOut(BaseModel):
    id: uuid.UUID
    scope: Literal["school", "platform"]
    title: str
    description: str | None
    subject: str | None
    stream: Stream | None
    grade: int | None
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    question_count: int
    status: ContestStatus
    participants: int
    can_join: bool
    my_entry: EntryOut | None


class ContestPlay(BaseModel):
    contest: ContestOut
    entry: EntryOut
    questions: list[QuestionPublic]


class ContestSubmit(BaseModel):
    answers: list[AnswerIn] = Field(max_length=30)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "ContestSubmit":
        ids = [a.question_id for a in self.answers]
        if len(set(ids)) != len(ids):
            raise ValueError("each question may be answered only once")
        return self


class LeaderboardRow(BaseModel):
    rank: int
    name: str
    school: str
    score: int
    total: int
    time_taken_seconds: int
    is_me: bool


class Leaderboard(BaseModel):
    contest_id: uuid.UUID
    status: ContestStatus
    participants: int
    rows: list[LeaderboardRow]
    me: LeaderboardRow | None


class ContestReview(BaseModel):
    contest: ContestOut
    items: list[ItemResult]


class WeeklyRow(BaseModel):
    rank: int
    name: str
    questions: int
    correct: int
    accuracy: float
    streak: int
    is_me: bool


class WeeklyLeaderboard(BaseModel):
    since: datetime
    rows: list[WeeklyRow]
    me: WeeklyRow | None
