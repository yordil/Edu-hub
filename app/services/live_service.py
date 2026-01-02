"""Real-time contest leaderboards: a Redis sorted set per contest (O(log N) updates and rank lookups)
plus pub/sub to push changes to connected WebSocket clients. PostgreSQL stays the source of truth;
the board is rebuilt from it whenever the Redis key is missing."""

import json
import logging
import uuid

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.models.contest import Contest, ContestEntry
from app.models.user import User
from app.schemas.contest import Leaderboard, LeaderboardRow
from app.services import contest_service

log = logging.getLogger("eduhub.live")

TOP_N = 10
TTL_SECONDS = 7 * 24 * 3600
# Composite sort key: more correct first, then less time. Time is capped so it never outweighs one point.
_TIME_SLOTS = 100_000


def keys(contest_id: uuid.UUID) -> tuple[str, str, str]:
    base = f"contest:{contest_id}"
    return f"{base}:board", f"{base}:names", f"{base}:events"


def composite(score: int, seconds: int) -> int:
    return score * _TIME_SLOTS - min(seconds, _TIME_SLOTS - 1)


def decompose(value: float) -> tuple[int, int]:
    v = int(value)
    score = (v + _TIME_SLOTS - 1) // _TIME_SLOTS
    return score, score * _TIME_SLOTS - v


def _member(c: Contest, e: ContestEntry) -> tuple[str, int, str]:
    meta = json.dumps({"name": contest_service._display_name(e, c), "school": e.user.organization.name})
    return str(e.user_id), composite(e.score or 0, e.time_taken_seconds or 0), meta


async def _rebuild(r, db: AsyncSession, c: Contest) -> None:
    board, names, _ = keys(c.id)
    entries = await contest_service._ranked(db, c.id)
    if not entries:
        return
    members = [_member(c, e) for e in entries]
    async with r.pipeline(transaction=True) as p:
        p.zadd(board, {m: s for m, s, _ in members})
        p.hset(names, mapping={m: meta for m, _, meta in members})
        p.expire(board, TTL_SECONDS)
        p.expire(names, TTL_SECONDS)
        await p.execute()


async def on_submit(db: AsyncSession, contest_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Update the live board and notify subscribers. Redis problems never fail the submission."""
    r = get_redis()
    if r is None:
        return
    c = await db.get(Contest, contest_id)
    entry = await db.scalar(select(ContestEntry).where(ContestEntry.contest_id == contest_id, ContestEntry.user_id == user_id))
    if c is None or entry is None or entry.submitted_at is None:
        return
    board, names, channel = keys(c.id)
    try:
        if not await r.exists(board):
            await _rebuild(r, db, c)
        member, score, meta = _member(c, entry)
        async with r.pipeline(transaction=True) as p:
            p.zadd(board, {member: score})
            p.hset(names, member, meta)
            p.expire(board, TTL_SECONDS)
            p.expire(names, TTL_SECONDS)
            p.publish(channel, json.dumps({"type": "update"}))
            await p.execute()
    except RedisError as e:
        log.warning("live board update failed", extra={"fields": {"error": type(e).__name__, "contest": str(contest_id)}})


def _row(rank: int, member: str, value: float, meta: str | None, total: int, me: uuid.UUID) -> LeaderboardRow:
    info = json.loads(meta) if meta else {"name": "Student", "school": ""}
    score, seconds = decompose(value)
    return LeaderboardRow(
        rank=rank, name=info["name"], school=info["school"], score=score, total=total, time_taken_seconds=seconds, is_me=member == str(me)
    )


async def snapshot(db: AsyncSession, contest_id: uuid.UUID, user: User) -> dict:
    """Top-N board plus the viewer's own position, from Redis when available, otherwise from PostgreSQL."""
    c = await db.get(Contest, contest_id)
    r = get_redis()
    if r is not None:
        try:
            board, names, _ = keys(c.id)
            if not await r.exists(board):
                await _rebuild(r, db, c)
            top = await r.zrevrange(board, 0, TOP_N - 1, withscores=True)
            participants = await r.zcard(board)
            my_rank = await r.zrevrank(board, str(user.id))
            wanted = [m for m, _ in top] + ([str(user.id)] if my_rank is not None and my_rank >= TOP_N else [])
            metas = dict(zip(wanted, await r.hmget(names, wanted))) if wanted else {}
            total = len(c.questions)
            rows = [_row(i + 1, m, s, metas.get(m), total, user.id) for i, (m, s) in enumerate(top)]
            me = next((row for row in rows if row.is_me), None)
            if me is None and my_rank is not None:
                me = _row(my_rank + 1, str(user.id), await r.zscore(board, str(user.id)), metas.get(str(user.id)), total, user.id)
            board_out = Leaderboard(
                contest_id=c.id, status=contest_service.status(c, contest_service._now()), participants=participants, rows=rows, me=me
            )
            return {"type": "board", "source": "redis", **board_out.model_dump(mode="json")}
        except RedisError as e:
            log.warning("live board read failed; using database", extra={"fields": {"error": type(e).__name__}})
    board_out = await contest_service.leaderboard(db, user, c.id)
    board_out.rows = board_out.rows[:TOP_N]
    return {"type": "board", "source": "database", **board_out.model_dump(mode="json")}
