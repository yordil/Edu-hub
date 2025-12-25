import uuid
from datetime import datetime

from pydantic import BaseModel

from app.core.curriculum import Stream
from app.schemas.quiz import Activity, AttemptSummary, MasteryOut, TopicStat
from app.schemas.user import UserOut


class ClassStudentRow(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    grade: int | None = None
    stream: Stream | None
    questions_7d: int
    questions_total: int
    accuracy: float | None
    avg_mastery: float | None
    weakest_topic: str | None
    current_streak: int
    last_active: datetime | None


class ClassTopicRow(BaseModel):
    subject: str
    topic: str
    students: int
    avg_mastery: float
    struggling: int


class ClassOverview(BaseModel):
    students: int
    active_7d: int
    questions_7d: int
    accuracy: float | None
    rows: list[ClassStudentRow]
    topics: list[ClassTopicRow]


class StudentDetail(BaseModel):
    student: UserOut
    mastery: list[MasteryOut]
    topics: list[TopicStat]
    activity: Activity
    attempts: list[AttemptSummary]
