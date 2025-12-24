"""Daily practice activity and streaks (GitHub/LeetCode-style). Days are calendar days in UTC."""

from datetime import date, timedelta

from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.quiz import Attempt, AttemptItem
from app.models.user import User
from app.schemas.quiz import Activity, ActivityDay


def streaks(active: set[date], today: date) -> tuple[int, int]:
    """(current, longest). The current streak stays alive until a full day is missed, so practising
    yesterday but not yet today still counts."""
    longest = run = 0
    prev: date | None = None
    for d in sorted(active):
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        longest = max(longest, run)
        prev = d

    cursor = today if today in active else today - timedelta(days=1)
    current = 0
    while cursor in active:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


async def activity(db: AsyncSession, user: User, days: int) -> Activity:
    today = utcnow().date()
    start = today - timedelta(days=days - 1)
    day = cast(Attempt.submitted_at, Date)
    submitted = (Attempt.user_id == user.id) & Attempt.submitted_at.is_not(None)

    rows = await db.execute(
        select(
            day,
            func.count(func.distinct(Attempt.id)),
            func.count(AttemptItem.id),
            func.sum(case((AttemptItem.is_correct.is_(True), 1), else_=0)),
        )
        .join(AttemptItem, AttemptItem.attempt_id == Attempt.id)
        .where(submitted, day >= start)
        .group_by(day)
    )
    by_day = {d: (s, q, int(c or 0)) for d, s, q, c in rows}
    series = [
        ActivityDay(date=d, sessions=by_day.get(d, (0, 0, 0))[0], questions=by_day.get(d, (0, 0, 0))[1], correct=by_day.get(d, (0, 0, 0))[2])
        for d in (start + timedelta(days=i) for i in range(days))
    ]

    # Streaks use the full history, not just the visible window.
    all_days = set(await db.scalars(select(day).where(submitted).distinct()))
    current, longest = streaks(all_days, today)
    return Activity(
        days=series,
        current_streak=current,
        longest_streak=longest,
        active_days=sum(1 for d in series if d.sessions),
        total_questions=sum(d.questions for d in series),
    )
