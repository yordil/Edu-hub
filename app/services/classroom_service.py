"""Teacher views of their school: per-student progress, class-wide weak topics, and the weekly leaderboard."""

import uuid
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.base import utcnow
from app.models.quiz import Attempt, AttemptItem, TopicMastery
from app.models.user import Role, User
from app.schemas.classroom import ClassOverview, ClassStudentRow, ClassTopicRow, StudentDetail
from app.schemas.contest import WeeklyLeaderboard, WeeklyRow
from app.schemas.user import UserOut
from app.services import activity_service, knowledge_model as km, learning_service, quiz_service

WEEKLY_SIZE = 20


async def _students(db: AsyncSession, org_id: uuid.UUID) -> list[User]:
    return list(
        await db.scalars(
            select(User).where(User.org_id == org_id, User.role == Role.student, User.is_active.is_(True)).order_by(User.full_name, User.id)
        )
    )


async def _answer_totals(db: AsyncSession, org_id: uuid.UUID, since=None) -> dict[uuid.UUID, tuple[int, int]]:
    """user_id -> (questions answered, correct) over submitted practice sessions."""
    stmt = (
        select(Attempt.user_id, func.count(AttemptItem.id), func.sum(case((AttemptItem.is_correct.is_(True), 1), else_=0)))
        .join(AttemptItem, AttemptItem.attempt_id == Attempt.id)
        .where(Attempt.org_id == org_id, Attempt.submitted_at.is_not(None))
        .group_by(Attempt.user_id)
    )
    if since is not None:
        stmt = stmt.where(Attempt.submitted_at >= since)
    return {u: (int(n), int(c or 0)) for u, n, c in await db.execute(stmt)}


async def _active_days(db: AsyncSession, org_id: uuid.UUID) -> dict[uuid.UUID, set[date]]:
    day = cast(Attempt.submitted_at, Date)
    rows = await db.execute(
        select(Attempt.user_id, day).where(Attempt.org_id == org_id, Attempt.submitted_at.is_not(None)).distinct()
    )
    out: dict[uuid.UUID, set[date]] = defaultdict(set)
    for user_id, d in rows:
        out[user_id].add(d)
    return out


async def overview(db: AsyncSession, staff: User) -> ClassOverview:
    now = utcnow()
    today = now.date()
    students = await _students(db, staff.org_id)
    ids = [s.id for s in students]
    totals = await _answer_totals(db, staff.org_id)
    week = await _answer_totals(db, staff.org_id, since=now - timedelta(days=7))
    days = await _active_days(db, staff.org_id)
    last_active = dict(
        (await db.execute(
            select(Attempt.user_id, func.max(Attempt.submitted_at)).where(Attempt.org_id == staff.org_id).group_by(Attempt.user_id)
        )).all()
    )
    mastery_rows = list(await db.scalars(select(TopicMastery).where(TopicMastery.user_id.in_(ids)))) if ids else []
    by_student: dict[uuid.UUID, list[TopicMastery]] = defaultdict(list)
    by_topic: dict[tuple[str, str], list[TopicMastery]] = defaultdict(list)
    for m in mastery_rows:
        by_student[m.user_id].append(m)
        by_topic[(m.subject, m.topic)].append(m)

    rows = []
    for s in students:
        n, c = totals.get(s.id, (0, 0))
        ms = by_student.get(s.id, [])
        weakest = min(ms, key=lambda m: (km.mastery(m.theta), -m.answered), default=None)
        rows.append(
            ClassStudentRow(
                id=s.id,
                full_name=s.full_name,
                email=s.email,
                grade=s.grade,
                stream=s.stream,
                questions_7d=week.get(s.id, (0, 0))[0],
                questions_total=n,
                accuracy=round(c / n, 4) if n else None,
                avg_mastery=round(sum(km.mastery(m.theta) for m in ms) / len(ms), 4) if ms else None,
                weakest_topic=weakest.topic if weakest else None,
                current_streak=activity_service.streaks(days.get(s.id, set()), today)[0],
                last_active=last_active.get(s.id),
            )
        )

    topics = sorted(
        (
            ClassTopicRow(
                subject=subject,
                topic=topic,
                students=len(ms),
                avg_mastery=round(sum(km.mastery(m.theta) for m in ms) / len(ms), 4),
                struggling=sum(1 for m in ms if km.level(m.theta, m.answered) == "struggling"),
            )
            for (subject, topic), ms in by_topic.items()
        ),
        key=lambda t: (t.avg_mastery, -t.students, t.subject, t.topic),
    )
    all_n = sum(n for n, _ in totals.values())
    all_c = sum(c for _, c in totals.values())
    return ClassOverview(
        students=len(students),
        active_7d=sum(1 for s in students if week.get(s.id, (0, 0))[0] > 0),
        questions_7d=sum(n for n, _ in week.values()),
        accuracy=round(all_c / all_n, 4) if all_n else None,
        rows=rows,
        topics=topics,
    )


async def student_detail(db: AsyncSession, staff: User, student_id: uuid.UUID) -> StudentDetail:
    student = await db.get(User, student_id)
    # Other schools' students (and staff) look like they don't exist.
    if student is None or student.org_id != staff.org_id or student.role is not Role.student:
        raise NotFoundError("Student not found")
    attempts = await quiz_service.list_attempts(db, student, 20, 0)
    return StudentDetail(
        student=UserOut.model_validate(student),
        mastery=await learning_service.list_mastery(db, student),
        topics=await quiz_service.topic_stats(db, student),
        activity=await activity_service.activity(db, student, 365),
        attempts=[quiz_service.to_summary(a) for a in attempts],
    )


async def weekly_leaderboard(db: AsyncSession, user: User) -> WeeklyLeaderboard:
    """This school's students ranked by correct answers in the last 7 days."""
    now = utcnow()
    since = now - timedelta(days=7)
    students = {s.id: s for s in await _students(db, user.org_id)}
    week = await _answer_totals(db, user.org_id, since=since)
    days = await _active_days(db, user.org_id)
    ranked = sorted(
        ((uid, n, c) for uid, (n, c) in week.items() if uid in students and n > 0),
        key=lambda t: (-t[2], -t[1], students[t[0]].full_name),
    )
    rows = [
        WeeklyRow(
            rank=i + 1,
            name=students[uid].full_name,
            questions=n,
            correct=c,
            accuracy=round(c / n, 4),
            streak=activity_service.streaks(days.get(uid, set()), now.date())[0],
            is_me=uid == user.id,
        )
        for i, (uid, n, c) in enumerate(ranked)
    ]
    return WeeklyLeaderboard(since=since, rows=rows[:WEEKLY_SIZE], me=next((r for r in rows if r.is_me), None))
