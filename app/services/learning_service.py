import uuid

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.base import utcnow
from app.models.quiz import Attempt, AttemptItem, Difficulty, Question, TopicMastery
from app.models.user import User
from app.schemas.quiz import MasteryOut, NextQuestion, QuestionPublic
from app.services import knowledge_model as km
from app.services.question_filters import filter_questions, student_scope

# Ranking is O(pool); sampling caps cost on large question banks.
CANDIDATE_POOL = 1000


async def load_thetas(db: AsyncSession, user_id: uuid.UUID) -> dict[tuple[str, str], float]:
    rows = await db.execute(
        select(TopicMastery.subject, TopicMastery.topic, TopicMastery.theta).where(TopicMastery.user_id == user_id)
    )
    return {(s, t): theta for s, t, theta in rows}


async def seen_questions(db: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, bool]:
    """question_id -> whether the student has ever answered it correctly (submitted attempts only)."""
    rows = await db.execute(
        select(AttemptItem.question_id, func.bool_or(AttemptItem.is_correct))
        .join(Attempt, Attempt.id == AttemptItem.attempt_id)
        .where(Attempt.user_id == user_id, Attempt.submitted_at.is_not(None))
        .group_by(AttemptItem.question_id)
    )
    return {qid: bool(ok) for qid, ok in rows}


async def rank_questions(
    db: AsyncSession,
    user: User,
    subject: str | None = None,
    topic: str | None = None,
    difficulty: Difficulty | None = None,
    grade: int | None = None,
) -> list[km.Ranked]:
    stmt = filter_questions(
        select(Question.id, Question.subject, Question.topic, Question.rating),
        user.org_id,
        subject=subject,
        topic=topic,
        difficulty=difficulty,
        grade=grade,
        **student_scope(user),
    )
    rows = await db.execute(stmt.order_by(func.random()).limit(CANDIDATE_POOL))
    candidates = [km.Candidate(id=i, subject=s, topic=t, rating=r) for i, s, t, r in rows]
    return km.rank(candidates, await load_thetas(db, user.id), await seen_questions(db, user.id))


async def load_questions_in_order(db: AsyncSession, ids: list[uuid.UUID]) -> list[Question]:
    by_id = {q.id: q for q in await db.scalars(select(Question).where(Question.id.in_(ids)))}
    return [by_id[i] for i in ids]


async def next_question(
    db: AsyncSession, user: User, subject: str | None, topic: str | None, grade: int | None = None
) -> NextQuestion:
    ranked = await rank_questions(db, user, subject, topic, grade=grade)
    if not ranked:
        raise NotFoundError("No questions match the given filters")
    best = ranked[0]
    question = await db.get(Question, best.candidate.id)
    return NextQuestion(
        question=QuestionPublic.model_validate(question),
        predicted_success=round(best.p, 4),
        mastery=round(best.mastery, 4),
        reason=km.explain(best),
    )


async def apply_attempt(db: AsyncSession, attempt: Attempt) -> None:
    """Update student skill and question difficulty from a freshly graded attempt (caller commits)."""
    keys = sorted({(i.question.subject, i.question.topic) for i in attempt.items})
    await db.execute(
        pg_insert(TopicMastery)
        .values([
            {"id": uuid.uuid4(), "user_id": attempt.user_id, "subject": s, "topic": t, "updated_at": utcnow()}
            for s, t in keys
        ])
        .on_conflict_do_nothing(constraint="uq_topic_mastery_user_subject_topic")
    )
    # Row locks serialize concurrent submissions by the same student on the same topics.
    rows = await db.scalars(
        select(TopicMastery)
        .where(TopicMastery.user_id == attempt.user_id, tuple_(TopicMastery.subject, TopicMastery.topic).in_(keys))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    mastery_by_key = {(m.subject, m.topic): m for m in rows}

    for item in attempt.items:
        q = item.question
        m = mastery_by_key[(q.subject, q.topic)]
        result = km.update(m.theta, m.answered, q.rating, q.rating_count, bool(item.is_correct))
        m.theta = result.theta
        m.answered += 1
        m.correct += int(bool(item.is_correct))
        # Atomic increment: many students update the same question concurrently.
        await db.execute(
            update(Question)
            .where(Question.id == q.id)
            .values(rating=Question.rating + result.beta_delta, rating_count=Question.rating_count + 1)
            .execution_options(synchronize_session=False)
        )


async def list_mastery(db: AsyncSession, user: User) -> list[MasteryOut]:
    rows = await db.scalars(select(TopicMastery).where(TopicMastery.user_id == user.id))
    out = [
        MasteryOut(
            subject=m.subject,
            topic=m.topic,
            mastery=round(km.mastery(m.theta), 4),
            level=km.level(m.theta, m.answered),
            answered=m.answered,
            correct=m.correct,
            updated_at=m.updated_at,
        )
        for m in rows
    ]
    return sorted(out, key=lambda m: (m.mastery, -m.answered, m.subject, m.topic))
