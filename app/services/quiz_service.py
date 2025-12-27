import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.models.quiz import Attempt, AttemptItem, AttemptMode, Choice, Difficulty, Question
from app.models.user import User
from app.schemas.quiz import (
    AttemptCreate,
    AttemptDetail,
    AttemptProgress,
    AttemptSubmit,
    AttemptSummary,
    ItemResult,
    QuestionCreate,
    QuestionPublic,
    TopicStat,
)
from app.services import knowledge_model as km
from app.services import learning_service, semantic_service
from app.services.question_filters import Scope, filter_questions, is_visible, student_scope

# ---------- Question bank ----------


def _build_question(data: QuestionCreate, author: User, org_id: uuid.UUID | None) -> Question:
    return Question(
        org_id=org_id,
        subject=data.subject,
        topic=data.topic,
        difficulty=data.difficulty,
        grade=data.grade,
        source=data.source.strip() if data.source else None,
        prompt=data.prompt,
        explanation=data.explanation,
        created_by_id=author.id,
        rating=km.INITIAL_QUESTION_RATING[data.difficulty],
        rating_count=0,
        choices=[Choice(position=i, text=c.text, is_correct=c.is_correct) for i, c in enumerate(data.choices)],
    )


async def create_question(db: AsyncSession, data: QuestionCreate, author: User) -> Question:
    question = _build_question(data, author, author.org_id)
    db.add(question)
    await db.commit()
    await semantic_service.embed_questions(db, [question])
    return question


async def create_library_questions(db: AsyncSession, items: list[QuestionCreate], author: User) -> list[Question]:
    """Add questions to the shared library every school can practice from. All-or-nothing."""
    return await create_questions(db, items, author, None)


async def create_questions(db: AsyncSession, items: list[QuestionCreate], author: User, org_id: uuid.UUID | None) -> list[Question]:
    """Bulk insert in one transaction (org_id None = shared library), then embed for search."""
    questions = [_build_question(data, author, org_id) for data in items]
    db.add_all(questions)
    await db.commit()
    await semantic_service.embed_questions(db, questions)
    return questions


async def list_questions(
    db: AsyncSession,
    org_id: uuid.UUID,
    subject: str | None,
    topic: str | None,
    difficulty: Difficulty | None,
    limit: int,
    offset: int,
    grade: int | None = None,
    scope: Scope = "all",
    subjects: frozenset[str] | None = None,
    max_grade: int | None = None,
) -> list[Question]:
    stmt = filter_questions(
        select(Question), org_id, subject=subject, topic=topic, difficulty=difficulty, grade=grade, scope=scope,
        subjects=subjects, max_grade=max_grade,
    )
    stmt = stmt.order_by(Question.created_at, Question.id).limit(limit).offset(offset)
    return list(await db.scalars(stmt))


async def get_question(db: AsyncSession, org_id: uuid.UUID, question_id: uuid.UUID) -> Question:
    question = await db.get(Question, question_id)
    # Other tenants' rows are reported as missing, not forbidden, so ids can't be probed.
    if question is None or not is_visible(question, org_id):
        raise NotFoundError("Question not found")
    return question


# ---------- Attempts ----------

EXAM_MINUTES_PER_QUESTION = 1.5
# Network slack for the client's auto-submit at 00:00; after this, only autosaved answers count.
EXAM_GRACE = timedelta(seconds=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired(attempt: Attempt) -> bool:
    return attempt.deadline is not None and _now() > attempt.deadline + EXAM_GRACE


async def start_attempt(db: AsyncSession, data: AttemptCreate, user: User) -> Attempt:
    if data.mode is AttemptMode.adaptive:
        ranked = await learning_service.rank_questions(
            db, user, data.subject, data.topic, data.difficulty, grade=data.grade
        )
        ranked = ranked[: data.size]
        questions = await learning_service.load_questions_in_order(db, [r.candidate.id for r in ranked])
        predictions = [r.p for r in ranked]
    else:
        stmt = filter_questions(
            select(Question),
            user.org_id,
            subject=data.subject,
            topic=data.topic,
            difficulty=data.difficulty,
            grade=data.grade,
            **student_scope(user),
        )
        questions = list(await db.scalars(stmt.order_by(func.random()).limit(data.size)))
        thetas = await learning_service.load_thetas(db, user.id)
        predictions = [km.predict(thetas.get((q.subject, q.topic), 0.0), q.rating) for q in questions]
    if not questions:
        raise NotFoundError("No questions match the given filters")

    deadline = None
    if data.mode is AttemptMode.exam:
        minutes = data.time_limit_minutes or math.ceil(len(questions) * EXAM_MINUTES_PER_QUESTION)
        deadline = _now() + timedelta(minutes=minutes)

    attempt = Attempt(
        org_id=user.org_id,
        user_id=user.id,
        mode=data.mode,
        subject=data.subject,
        topic=data.topic,
        difficulty=data.difficulty,
        deadline=deadline,
        items=[
            AttemptItem(question=q, position=i, predicted_p=p)
            for i, (q, p) in enumerate(zip(questions, predictions))
        ],
    )
    db.add(attempt)
    await db.commit()
    return attempt


async def get_attempt(db: AsyncSession, attempt_id: uuid.UUID, user: User) -> Attempt:
    attempt = await db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise NotFoundError("Attempt not found")
    if attempt.submitted_at is None and _expired(attempt):
        # Time ran out without a submit (tab closed, offline): close it with the autosaved answers.
        attempt = await db.get(Attempt, attempt_id, with_for_update=True, populate_existing=True)
        if attempt.submitted_at is None:
            await _grade(db, attempt)
    return attempt


async def _locked_open_attempt(db: AsyncSession, attempt_id: uuid.UUID, user: User) -> Attempt:
    # Row lock stops two concurrent writers (submit/autosave) from racing on the same attempt.
    attempt = await db.get(Attempt, attempt_id, with_for_update=True)
    if attempt is None or attempt.user_id != user.id:
        raise NotFoundError("Attempt not found")
    if attempt.submitted_at is not None:
        raise ConflictError("Attempt already submitted")
    return attempt


def _check_choice(item: AttemptItem, choice_id: uuid.UUID) -> None:
    if choice_id not in {c.id for c in item.question.choices}:
        raise ValidationFailed(f"Choice {choice_id} does not belong to question {item.question_id}")


def _item_for(items_by_question: dict[uuid.UUID, AttemptItem], question_id: uuid.UUID) -> AttemptItem:
    item = items_by_question.get(question_id)
    if item is None:
        raise ValidationFailed(f"Question {question_id} is not part of this attempt")
    return item


async def save_progress(db: AsyncSession, attempt_id: uuid.UUID, data: AttemptProgress, user: User) -> Attempt:
    """Autosave answers and flags so a refresh, crash or timeout never loses work. Nothing is graded here."""
    attempt = await _locked_open_attempt(db, attempt_id, user)
    if _expired(attempt):
        raise ConflictError("Time is up for this exam")
    items_by_question = {item.question_id: item for item in attempt.items}
    for p in data.items:
        item = _item_for(items_by_question, p.question_id)
        if p.choice_id is not None:
            _check_choice(item, p.choice_id)
        item.selected_choice_id = p.choice_id
        item.flagged = p.flagged
    await db.commit()
    return attempt


async def submit_attempt(db: AsyncSession, attempt_id: uuid.UUID, data: AttemptSubmit, user: User) -> Attempt:
    attempt = await _locked_open_attempt(db, attempt_id, user)
    items_by_question = {item.question_id: item for item in attempt.items}
    # After the deadline only autosaved answers count, so a late request can't add answers.
    if not _expired(attempt):
        for answer in data.answers:
            item = _item_for(items_by_question, answer.question_id)
            _check_choice(item, answer.choice_id)
            item.selected_choice_id = answer.choice_id
    await _grade(db, attempt)
    return attempt


async def _grade(db: AsyncSession, attempt: Attempt) -> None:
    for item in attempt.items:
        item.is_correct = item.selected_choice_id == item.question.correct_choice.id
    attempt.score = sum(1 for item in attempt.items if item.is_correct)
    attempt.submitted_at = _now()
    await learning_service.apply_attempt(db, attempt)
    await db.commit()


async def list_attempts(db: AsyncSession, user: User, limit: int, offset: int) -> list[Attempt]:
    stmt = (
        select(Attempt)
        .where(Attempt.user_id == user.id)
        .order_by(Attempt.started_at.desc(), Attempt.id)
        .limit(limit)
        .offset(offset)
    )
    return list(await db.scalars(stmt))


async def topic_stats(db: AsyncSession, user: User) -> list[TopicStat]:
    """Per-topic accuracy over submitted attempts, weakest topics first."""
    correct = func.sum(case((AttemptItem.is_correct.is_(True), 1), else_=0))
    stmt = (
        select(Question.subject, Question.topic, func.count(AttemptItem.id), correct)
        .select_from(AttemptItem)
        .join(Question, Question.id == AttemptItem.question_id)
        .join(Attempt, Attempt.id == AttemptItem.attempt_id)
        .where(Attempt.user_id == user.id, Attempt.submitted_at.is_not(None))
        .group_by(Question.subject, Question.topic)
    )
    rows = (await db.execute(stmt)).all()
    stats = [
        TopicStat(subject=s, topic=t, answered=n, correct=c, accuracy=round(c / n, 4))
        for s, t, n, c in rows
    ]
    return sorted(stats, key=lambda s: (s.accuracy, -s.answered, s.subject, s.topic))


# ---------- Serialization ----------


def to_summary(attempt: Attempt) -> AttemptSummary:
    return AttemptSummary(
        id=attempt.id,
        mode=attempt.mode,
        subject=attempt.subject,
        topic=attempt.topic,
        difficulty=attempt.difficulty,
        started_at=attempt.started_at,
        deadline=attempt.deadline,
        submitted_at=attempt.submitted_at,
        score=attempt.score,
        total=attempt.total,
    )


def to_detail(attempt: Attempt) -> AttemptDetail:
    revealed = attempt.submitted_at is not None
    items = [
        ItemResult(
            question=QuestionPublic.model_validate(item.question),
            selected_choice_id=item.selected_choice_id,
            flagged=item.flagged,
            predicted_success=round(item.predicted_p, 4) if item.predicted_p is not None else None,
            correct_choice_id=item.question.correct_choice.id if revealed else None,
            is_correct=item.is_correct if revealed else None,
            explanation=(item.question.explanation or item.question.ai_explanation) if revealed else None,
            ai_check=item.question.ai_check if revealed else None,
        )
        for item in attempt.items
    ]
    return AttemptDetail(**to_summary(attempt).model_dump(), items=items)
