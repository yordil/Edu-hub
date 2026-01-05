"""Weekly contests: fixed question sets, a shared time window, one graded entry per student, ranked leaderboards."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.curriculum import COMMON_SUBJECTS, STREAM_SUBJECTS
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailed
from app.models.base import utcnow
from app.models.contest import Contest, ContestAnswer, ContestEntry, ContestQuestion
from app.models.quiz import Question
from app.models.user import Role, User
from app.schemas.contest import (
    ContestCreate,
    ContestOut,
    ContestPlay,
    ContestReview,
    ContestStatus,
    ContestSubmit,
    EntryOut,
    Leaderboard,
    LeaderboardRow,
)
from app.schemas.quiz import ItemResult, QuestionPublic
from app.services.question_filters import filter_questions

# Network/clock slack so an auto-submit fired at 0:00 on the client still counts.
GRACE = timedelta(seconds=20)
LEADERBOARD_SIZE = 100


def _now() -> datetime:
    return utcnow()


def status(c: Contest, now: datetime) -> ContestStatus:
    if now < c.starts_at:
        return "upcoming"
    return "live" if now < c.ends_at else "ended"


def deadline(c: Contest, e: ContestEntry) -> datetime:
    return min(e.started_at + timedelta(minutes=c.duration_minutes), c.ends_at)


def rank_key(e: ContestEntry) -> tuple:
    """Most correct first, then fastest, then earliest submission."""
    return (-(e.score or 0), e.time_taken_seconds or 0, e.submitted_at, str(e.id))


def _ineligibility(c: Contest, user: User) -> str | None:
    """Why this user can't enter, or None if they can.

    Contests are for students. Staff host them: a teacher who entered would sit at the top of the
    student leaderboard and push a real student off the podium, which is the opposite of the point.
    The weekly leaderboard counts students only for the same reason.
    """
    if user.role is not Role.student:
        return "Teachers and admins host contests rather than take part. Follow the live leaderboard instead."
    if c.grade is not None and user.grade is not None and c.grade > user.grade:
        return f"This contest is for grade {c.grade} students"
    if c.stream is not None and user.stream not in (None, c.stream):
        return f"This contest is for the {c.stream.value} science stream"
    return None


def _eligible(c: Contest, user: User) -> bool:
    return _ineligibility(c, user) is None


# ---------- creation ----------


async def create(db: AsyncSession, author: User, data: ContestCreate, platform_wide: bool = False) -> Contest:
    if data.subject:
        subjects = frozenset({data.subject})
    elif data.stream:
        subjects = STREAM_SUBJECTS[data.stream]
    elif platform_wide:
        # Open to both streams, so only subjects every student studies.
        subjects = COMMON_SUBJECTS
    else:
        subjects = None
    stmt = filter_questions(
        select(Question.id),
        author.org_id,
        grade=data.grade,
        scope="library" if platform_wide else "all",
        subjects=subjects,
    )
    ids = list(await db.scalars(stmt.order_by(func.random()).limit(data.question_count)))
    if len(ids) < data.question_count:
        raise ValidationFailed(
            f"Only {len(ids)} question(s) match these filters; lower the question count or broaden the subject/grade."
        )
    contest = Contest(
        org_id=None if platform_wide else author.org_id,
        created_by_id=author.id,
        title=data.title.strip(),
        description=(data.description or "").strip() or None,
        subject=data.subject,
        stream=data.stream,
        grade=data.grade,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        duration_minutes=data.duration_minutes,
        questions=[ContestQuestion(question_id=qid, position=i) for i, qid in enumerate(ids)],
    )
    db.add(contest)
    await db.commit()
    # Reload so contest.questions[*].question is populated (async sessions can't lazy-load it later).
    return await db.get(Contest, contest.id, populate_existing=True)


# ---------- reading ----------


async def _get_visible(db: AsyncSession, user: User, contest_id: uuid.UUID) -> Contest:
    c = await db.get(Contest, contest_id)
    if c is None or (c.org_id is not None and c.org_id != user.org_id):
        raise NotFoundError("Contest not found")
    return c


async def _entry(db: AsyncSession, contest_id: uuid.UUID, user_id: uuid.UUID) -> ContestEntry | None:
    return await db.scalar(select(ContestEntry).where(ContestEntry.contest_id == contest_id, ContestEntry.user_id == user_id))


async def _finalize_if_expired(db: AsyncSession, c: Contest, e: ContestEntry, now: datetime) -> None:
    """An entry whose time ran out without a submission is closed with no answers."""
    if e.submitted_at is None and now > deadline(c, e) + GRACE:
        e.submitted_at = deadline(c, e)
        e.score = 0
        e.time_taken_seconds = int((deadline(c, e) - e.started_at).total_seconds())
        await db.commit()


async def _ranked(db: AsyncSession, contest_id: uuid.UUID) -> list[ContestEntry]:
    entries = list(
        await db.scalars(select(ContestEntry).where(ContestEntry.contest_id == contest_id, ContestEntry.submitted_at.is_not(None)))
    )
    return sorted(entries, key=rank_key)


def _entry_out(c: Contest, e: ContestEntry, rank: int | None) -> EntryOut:
    return EntryOut(
        started_at=e.started_at,
        deadline=deadline(c, e),
        submitted_at=e.submitted_at,
        score=e.score,
        total=len(c.questions),
        time_taken_seconds=e.time_taken_seconds,
        rank=rank,
    )


async def to_out(db: AsyncSession, c: Contest, user: User, now: datetime, entry: ContestEntry | None = None, participants: int | None = None) -> ContestOut:
    if entry is None:
        entry = await _entry(db, c.id, user.id)
    if participants is None:
        participants = await db.scalar(select(func.count()).select_from(ContestEntry).where(ContestEntry.contest_id == c.id)) or 0
    rank = None
    if entry is not None and entry.submitted_at is not None:
        ranked = await _ranked(db, c.id)
        rank = next(i + 1 for i, r in enumerate(ranked) if r.id == entry.id)
    st = status(c, now)
    return ContestOut(
        id=c.id,
        scope="platform" if c.org_id is None else "school",
        title=c.title,
        description=c.description,
        subject=c.subject,
        stream=c.stream,
        grade=c.grade,
        starts_at=c.starts_at,
        ends_at=c.ends_at,
        duration_minutes=c.duration_minutes,
        question_count=len(c.questions),
        status=st,
        participants=participants,
        can_join=st == "live" and entry is None and _eligible(c, user),
        my_entry=_entry_out(c, entry, rank) if entry else None,
    )


async def list_for(db: AsyncSession, user: User) -> list[ContestOut]:
    now = _now()
    contests = list(
        await db.scalars(
            select(Contest)
            .where(or_(Contest.org_id == user.org_id, Contest.org_id.is_(None)), Contest.ends_at >= now - timedelta(days=60))
            .order_by(Contest.starts_at.desc())
            .limit(50)
        )
    )
    ids = [c.id for c in contests]
    counts = dict(
        (await db.execute(select(ContestEntry.contest_id, func.count()).where(ContestEntry.contest_id.in_(ids)).group_by(ContestEntry.contest_id))).all()
    ) if ids else {}
    mine = {e.contest_id: e for e in await db.scalars(select(ContestEntry).where(ContestEntry.user_id == user.id, ContestEntry.contest_id.in_(ids)))} if ids else {}
    for c in contests:
        if c.id in mine:
            await _finalize_if_expired(db, c, mine[c.id], now)

    out = [await to_out(db, c, user, now, entry=mine.get(c.id), participants=counts.get(c.id, 0)) for c in contests]
    order = {"live": 0, "upcoming": 1, "ended": 2}
    # Live first, then soonest upcoming, then most recently ended.
    return sorted(
        out,
        key=lambda o: (order[o.status], o.starts_at.timestamp() if o.status == "upcoming" else -o.ends_at.timestamp()),
    )


async def get(db: AsyncSession, user: User, contest_id: uuid.UUID) -> ContestOut:
    now = _now()
    c = await _get_visible(db, user, contest_id)
    entry = await _entry(db, c.id, user.id)
    if entry:
        await _finalize_if_expired(db, c, entry, now)
    return await to_out(db, c, user, now, entry=entry)


# ---------- playing ----------


def _play(c: Contest, e: ContestEntry, out: ContestOut) -> ContestPlay:
    return ContestPlay(
        contest=out,
        entry=_entry_out(c, e, None),
        questions=[QuestionPublic.model_validate(cq.question) for cq in c.questions],
    )


async def start(db: AsyncSession, user: User, contest_id: uuid.UUID) -> ContestPlay:
    now = _now()
    c = await _get_visible(db, user, contest_id)
    entry = await _entry(db, c.id, user.id)
    if entry is not None:
        await _finalize_if_expired(db, c, entry, now)
        if entry.submitted_at is not None:
            raise ConflictError("You've already competed in this contest")
        return _play(c, entry, await to_out(db, c, user, now, entry=entry))

    st = status(c, now)
    if st == "upcoming":
        raise ConflictError("This contest hasn't started yet")
    if st == "ended":
        raise ConflictError("This contest has ended")
    if (reason := _ineligibility(c, user)) is not None:
        raise ForbiddenError(reason)

    entry = ContestEntry(contest_id=c.id, user_id=user.id, org_id=user.org_id, started_at=now)
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request (double click) created it first; resume that one.
        await db.rollback()
        entry = await _entry(db, c.id, user.id)
    return _play(c, entry, await to_out(db, c, user, now, entry=entry))


async def play(db: AsyncSession, user: User, contest_id: uuid.UUID) -> ContestPlay:
    """Resume an in-progress entry (e.g. after a page refresh)."""
    now = _now()
    c = await _get_visible(db, user, contest_id)
    entry = await _entry(db, c.id, user.id)
    if entry is not None:
        await _finalize_if_expired(db, c, entry, now)
    if entry is None or entry.submitted_at is not None:
        raise NotFoundError("No contest in progress")
    return _play(c, entry, await to_out(db, c, user, now, entry=entry))


async def submit(db: AsyncSession, user: User, contest_id: uuid.UUID, data: ContestSubmit) -> ContestOut:
    now = _now()
    c = await _get_visible(db, user, contest_id)
    entry = await db.scalar(
        select(ContestEntry)
        .where(ContestEntry.contest_id == c.id, ContestEntry.user_id == user.id)
        .with_for_update(of=ContestEntry)
        .execution_options(populate_existing=True)
    )
    if entry is None:
        raise NotFoundError("Start the contest first")
    if entry.submitted_at is not None:
        raise ConflictError("Already submitted")
    if now > deadline(c, entry) + GRACE:
        await _finalize_if_expired(db, c, entry, now)
        raise ConflictError("Time is up: your answers arrived after the deadline")

    by_question = {cq.question_id: cq.question for cq in c.questions}
    picked: dict[uuid.UUID, uuid.UUID] = {}
    for a in data.answers:
        q = by_question.get(a.question_id)
        if q is None:
            raise ValidationFailed(f"Question {a.question_id} is not part of this contest")
        if a.choice_id not in {ch.id for ch in q.choices}:
            raise ValidationFailed(f"Choice {a.choice_id} does not belong to question {a.question_id}")
        picked[q.id] = a.choice_id

    entry.answers = [
        ContestAnswer(question_id=qid, choice_id=picked.get(qid), is_correct=picked.get(qid) == q.correct_choice.id)
        for qid, q in by_question.items()
    ]
    entry.score = sum(1 for a in entry.answers if a.is_correct)
    entry.submitted_at = now
    entry.time_taken_seconds = max(0, int((min(now, deadline(c, entry)) - entry.started_at).total_seconds()))
    await db.commit()
    return await to_out(db, c, user, now, entry=entry)


# ---------- results ----------


def _display_name(e: ContestEntry, c: Contest) -> str:
    if c.org_id is not None:
        return e.user.full_name
    # Platform-wide boards are public across schools: first name + initial only.
    parts = e.user.full_name.split()
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."


async def leaderboard(db: AsyncSession, user: User, contest_id: uuid.UUID) -> Leaderboard:
    now = _now()
    c = await _get_visible(db, user, contest_id)
    ranked = await _ranked(db, c.id)
    participants = await db.scalar(select(func.count()).select_from(ContestEntry).where(ContestEntry.contest_id == c.id)) or 0
    total = len(c.questions)
    rows = [
        LeaderboardRow(
            rank=i + 1,
            name=_display_name(e, c),
            school=e.user.organization.name,
            score=e.score or 0,
            total=total,
            time_taken_seconds=e.time_taken_seconds or 0,
            is_me=e.user_id == user.id,
        )
        for i, e in enumerate(ranked)
    ]
    return Leaderboard(
        contest_id=c.id,
        status=status(c, now),
        participants=participants,
        rows=rows[:LEADERBOARD_SIZE],
        me=next((r for r in rows if r.is_me), None),
    )


async def review(db: AsyncSession, user: User, contest_id: uuid.UUID) -> ContestReview:
    """Answers are revealed only after the contest closes, so they can't be shared while it's live."""
    now = _now()
    c = await _get_visible(db, user, contest_id)
    if status(c, now) != "ended":
        raise ConflictError("Answers are revealed when the contest ends")
    entry = await _entry(db, c.id, user.id)
    picked = {a.question_id: a.choice_id for a in entry.answers} if entry else {}
    items = [
        ItemResult(
            question=QuestionPublic.model_validate(cq.question),
            selected_choice_id=picked.get(cq.question_id),
            correct_choice_id=cq.question.correct_choice.id,
            is_correct=picked.get(cq.question_id) == cq.question.correct_choice.id if entry else None,
            explanation=cq.question.explanation,
        )
        for cq in c.questions
    ]
    return ContestReview(contest=await to_out(db, c, user, now, entry=entry), items=items)
