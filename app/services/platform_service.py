from datetime import date, timedelta

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.organization import Organization
from app.models.quiz import Attempt, AttemptItem, Question
from app.models.tutor import AIUsage, GuardrailEvent, TutorConversation
from app.models.user import Role, User
from app.schemas.platform import (
    Activity,
    AIDay,
    AIMetrics,
    AIModelRow,
    DailyPoint,
    GuardrailRow,
    PlatformMetrics,
    PlatformTotals,
    RoleCounts,
    SchoolRow,
    SubjectRow,
)

DAILY_WINDOW = 30
SCHOOL_LIMIT = 200


def _ms(v) -> int | None:
    return None if v is None else int(round(float(v)))


async def ai_metrics(db: AsyncSession, days: int) -> AIMetrics:
    """Platform-wide AI observability over the last `days` days."""
    since = utcnow() - timedelta(days=days)
    recent = AIUsage.created_at >= since
    ok = AIUsage.outcome == "ok"
    tokens = AIUsage.input_tokens + AIUsage.output_tokens
    p50 = func.percentile_cont(0.5).within_group(AIUsage.latency_ms)
    p95 = func.percentile_cont(0.95).within_group(AIUsage.latency_ms)

    calls, errors, tin, tout = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(AIUsage.outcome != "ok"),
                func.coalesce(func.sum(AIUsage.input_tokens), 0),
                func.coalesce(func.sum(AIUsage.output_tokens), 0),
            ).where(recent)
        )
    ).one()
    lat50, lat95 = (await db.execute(select(p50, p95).where(recent, ok))).one()

    day = cast(AIUsage.created_at, Date)
    by_day = {
        d: (c, e, t, q)
        for d, c, e, t, q in await db.execute(
            select(day, func.count(), func.count().filter(AIUsage.outcome != "ok"), func.coalesce(func.sum(tokens), 0), p95)
            .where(recent)
            .group_by(day)
        )
    }
    today = utcnow().date()
    daily = [
        AIDay(day=d, calls=by_day.get(d, (0, 0, 0, None))[0], errors=by_day.get(d, (0, 0, 0, None))[1],
              tokens=int(by_day.get(d, (0, 0, 0, None))[2]), p95_ms=_ms(by_day.get(d, (0, 0, 0, None))[3]))
        for d in (today - timedelta(days=i) for i in range(days - 1, -1, -1))
    ]
    models = [
        AIModelRow(model=m, calls=c, tokens=int(t), p50_ms=_ms(l))
        for m, c, t, l in await db.execute(
            select(AIUsage.model, func.count(), func.coalesce(func.sum(tokens), 0), p50)
            .where(recent)
            .group_by(AIUsage.model)
            .order_by(func.count().desc())
        )
    ]
    guards = [
        GuardrailRow(category=c, stage=s, count=n)
        for c, s, n in await db.execute(
            select(GuardrailEvent.category, GuardrailEvent.stage, func.count())
            .where(GuardrailEvent.created_at >= since)
            .group_by(GuardrailEvent.category, GuardrailEvent.stage)
            .order_by(func.count().desc(), GuardrailEvent.category)
        )
    ]
    conversations = await db.scalar(select(func.count()).select_from(TutorConversation).where(TutorConversation.updated_at >= since))
    return AIMetrics(
        days=days,
        calls=calls,
        errors=errors,
        error_rate=round(errors / calls, 4) if calls else None,
        input_tokens=int(tin),
        output_tokens=int(tout),
        p50_ms=_ms(lat50),
        p95_ms=_ms(lat95),
        conversations=conversations or 0,
        daily=daily,
        models=models,
        guardrails=guards,
    )


def _days(today: date) -> list[date]:
    return [today - timedelta(days=i) for i in range(DAILY_WINDOW - 1, -1, -1)]


async def metrics(db: AsyncSession) -> PlatformMetrics:
    """Cross-school metrics for EduHub operators. The internal staff workspace is excluded throughout."""
    now = utcnow()
    d7, d30 = now - timedelta(days=7), now - timedelta(days=30)

    schools_q = select(Organization.id).where(Organization.is_internal.is_(False))
    in_schools_user = User.org_id.in_(schools_q)
    in_schools_attempt = Attempt.org_id.in_(schools_q)
    submitted = Attempt.submitted_at.is_not(None)

    async def count(stmt) -> int:
        return int(await db.scalar(stmt) or 0)

    roles = dict(
        (await db.execute(select(User.role, func.count()).where(in_schools_user).group_by(User.role))).all()
    )
    correct_expr = func.sum(case((AttemptItem.is_correct.is_(True), 1), else_=0))
    graded, correct = (
        await db.execute(
            select(func.count(AttemptItem.id), correct_expr)
            .join(Attempt, Attempt.id == AttemptItem.attempt_id)
            .where(submitted, in_schools_attempt)
        )
    ).one()
    graded, correct = int(graded or 0), int(correct or 0)

    totals = PlatformTotals(
        schools=await count(select(func.count()).select_from(Organization).where(Organization.is_internal.is_(False))),
        users=RoleCounts(
            students=roles.get(Role.student, 0), teachers=roles.get(Role.teacher, 0), admins=roles.get(Role.admin, 0)
        ),
        library_questions=await count(select(func.count()).select_from(Question).where(Question.org_id.is_(None))),
        school_questions=await count(select(func.count()).select_from(Question).where(Question.org_id.in_(schools_q))),
        attempts_submitted=await count(select(func.count()).select_from(Attempt).where(submitted, in_schools_attempt)),
        answers_graded=graded,
        accuracy=round(correct / graded, 4) if graded else None,
        ai_tokens_30d=await count(
            select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(
                AIUsage.org_id.in_(schools_q), AIUsage.created_at >= d30
            )
        ),
    )

    def active_since(since):
        return select(func.count(func.distinct(Attempt.user_id))).where(in_schools_attempt, Attempt.submitted_at >= since)

    def attempts_since(since):
        return select(func.count()).select_from(Attempt).where(in_schools_attempt, Attempt.submitted_at >= since)

    activity = Activity(
        new_schools_30d=await count(
            select(func.count()).select_from(Organization).where(Organization.is_internal.is_(False), Organization.created_at >= d30)
        ),
        new_students_30d=await count(
            select(func.count()).select_from(User).where(in_schools_user, User.role == Role.student, User.created_at >= d30)
        ),
        active_students_7d=await count(active_since(d7)),
        active_students_30d=await count(active_since(d30)),
        attempts_7d=await count(attempts_since(d7)),
        attempts_30d=await count(attempts_since(d30)),
    )

    window_start = now - timedelta(days=DAILY_WINDOW)
    attempt_day = cast(Attempt.submitted_at, Date)
    signup_day = cast(User.created_at, Date)
    attempts_by_day = dict(
        (await db.execute(
            select(attempt_day, func.count()).where(in_schools_attempt, Attempt.submitted_at >= window_start).group_by(attempt_day)
        )).all()
    )
    signups_by_day = dict(
        (await db.execute(
            select(signup_day, func.count()).where(in_schools_user, User.created_at >= window_start).group_by(signup_day)
        )).all()
    )
    daily = [
        DailyPoint(day=d, attempts=attempts_by_day.get(d, 0), signups=signups_by_day.get(d, 0))
        for d in _days(now.date())
    ]

    def per_school(stmt):
        return stmt.correlate(Organization).scalar_subquery()

    students = per_school(select(func.count()).where(User.org_id == Organization.id, User.role == Role.student))
    teachers = per_school(select(func.count()).where(User.org_id == Organization.id, User.role == Role.teacher))
    own_q = per_school(select(func.count()).where(Question.org_id == Organization.id))
    attempts = per_school(select(func.count()).where(Attempt.org_id == Organization.id, submitted))
    active = per_school(
        select(func.count(func.distinct(Attempt.user_id))).where(Attempt.org_id == Organization.id, Attempt.submitted_at >= d30)
    )
    last = per_school(select(func.max(Attempt.submitted_at)).where(Attempt.org_id == Organization.id))
    ai_tokens = per_school(
        select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(
            AIUsage.org_id == Organization.id, AIUsage.created_at >= d30
        )
    )
    rows = await db.execute(
        select(Organization.id, Organization.name, Organization.created_at, students, teachers, own_q, attempts, active, ai_tokens, last)
        .where(Organization.is_internal.is_(False))
        .order_by(Organization.created_at.desc(), Organization.id)
        .limit(SCHOOL_LIMIT)
    )
    schools = [
        SchoolRow(
            id=i, name=n, created_at=c, students=s, teachers=t, own_questions=q, attempts=a,
            active_students_30d=act, ai_tokens_30d=ai, last_activity=la,
        )
        for i, n, c, s, t, q, a, act, ai, la in rows
    ]

    subject_rows = await db.execute(
        select(Question.subject, func.count(AttemptItem.id), correct_expr)
        .select_from(AttemptItem)
        .join(Question, Question.id == AttemptItem.question_id)
        .join(Attempt, Attempt.id == AttemptItem.attempt_id)
        .where(submitted, in_schools_attempt)
        .group_by(Question.subject)
        .order_by(func.count(AttemptItem.id).desc(), Question.subject)
        .limit(10)
    )
    subjects = [SubjectRow(subject=s, answered=n, accuracy=round(c / n, 4)) for s, n, c in subject_rows]

    return PlatformMetrics(generated_at=now, totals=totals, activity=activity, daily=daily, schools=schools, subjects=subjects)
