import uuid
from typing import Literal

from sqlalchemy import Select, or_

from app.core.curriculum import allowed_subjects, normalize_subject, normalize_tag
from app.models.quiz import Difficulty, Question
from app.models.user import Role, User

Scope = Literal["all", "school", "library"]


def stream_subjects(user: User) -> frozenset[str] | None:
    return allowed_subjects(user.role.value, user.stream)


def grade_ceiling(user: User) -> int | None:
    """The highest grade of content a student may draw. None means no ceiling.

    A ceiling rather than an exact match, because the grade 12 entrance exam draws on earlier
    grades: a grade 12 student needs the whole bank, a grade 9 student only grade 9.

    Above-grade content is hidden for a reason beyond relevance. The knowledge model updates the
    *question's* difficulty from every answer, so a grade 9 student missing a grade 12 question
    would teach the model that question is harder than it is — for every student who sees it.
    Material a student hasn't been taught yet is noise in that signal.
    """
    return user.grade if user.role is Role.student else None


def student_scope(user: User) -> dict:
    """Everything that bounds what a student may draw, as one unit.

    Subjects and grade travel together so a new call site cannot apply one and forget the other.
    """
    return {"subjects": stream_subjects(user), "max_grade": grade_ceiling(user)}


def filter_questions(
    stmt: Select,
    org_id: uuid.UUID,
    *,
    subject: str | None = None,
    topic: str | None = None,
    difficulty: Difficulty | None = None,
    grade: int | None = None,
    scope: Scope = "all",
    subjects: frozenset[str] | None = None,
    max_grade: int | None = None,
) -> Select:
    """A school sees its own questions plus the shared library; never another school's.
    `subjects` restricts to a stream's subjects; `max_grade` hides content above a student's grade
    (untagged questions stay visible). `grade` is an explicit exact-match filter the caller chose."""
    if scope == "school":
        stmt = stmt.where(Question.org_id == org_id)
    elif scope == "library":
        stmt = stmt.where(Question.org_id.is_(None))
    else:
        stmt = stmt.where(or_(Question.org_id == org_id, Question.org_id.is_(None)))
    if subjects is not None:
        stmt = stmt.where(Question.subject.in_(sorted(subjects)))
    if subject:
        stmt = stmt.where(Question.subject == normalize_subject(subject))
    if topic:
        stmt = stmt.where(Question.topic == normalize_tag(topic))
    if difficulty:
        stmt = stmt.where(Question.difficulty == difficulty)
    if grade:
        stmt = stmt.where(Question.grade == grade)
    if max_grade is not None:
        stmt = stmt.where(or_(Question.grade.is_(None), Question.grade <= max_grade))
    return stmt


def is_visible(question: Question, org_id: uuid.UUID) -> bool:
    return question.org_id is None or question.org_id == org_id
