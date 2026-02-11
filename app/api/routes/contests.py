import asyncio
import uuid

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.api.deps import CurrentUser, DbSession, StaffUser
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import NotFoundError
from app.core.redis import get_redis
from app.core.security import create_ws_ticket, decode_ws_ticket
from app.models.user import User
from app.schemas.classroom import ClassOverview, StudentDetail
from app.schemas.contest import (
    ContestCreate,
    ContestOut,
    ContestPlay,
    ContestReview,
    ContestSubmit,
    Leaderboard,
    WeeklyLeaderboard,
)
from app.services import classroom_service, contest_service, live_service

router = APIRouter(prefix="/contests", tags=["contests"])


@router.get("", response_model=list[ContestOut])
async def list_contests(user: CurrentUser, db: DbSession):
    """Your school's contests plus EduHub-wide ones: live first, then upcoming, then recently ended."""
    return await contest_service.list_for(db, user)


@router.post("", response_model=ContestOut, status_code=status.HTTP_201_CREATED)
async def create_contest(data: ContestCreate, user: StaffUser, db: DbSession):
    """Teachers create a contest for their school; questions are drawn once so everyone gets the same set."""
    contest = await contest_service.create(db, user, data)
    return await contest_service.get(db, user, contest.id)


@router.get("/{contest_id}", response_model=ContestOut)
async def get_contest(contest_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return await contest_service.get(db, user, contest_id)


@router.post("/{contest_id}/start", response_model=ContestPlay)
async def start_contest(contest_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Join a live contest (once). Your personal timer starts now; calling again resumes the same entry."""
    return await contest_service.start(db, user, contest_id)


@router.get("/{contest_id}/play", response_model=ContestPlay)
async def resume_contest(contest_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return await contest_service.play(db, user, contest_id)


@router.post("/{contest_id}/submit", response_model=ContestOut)
async def submit_contest(contest_id: uuid.UUID, data: ContestSubmit, user: CurrentUser, db: DbSession):
    out = await contest_service.submit(db, user, contest_id, data)
    await live_service.on_submit(db, contest_id, user.id)
    return out


@router.post("/{contest_id}/live-ticket")
async def live_ticket(contest_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict:
    """A 60-second ticket for opening the live leaderboard WebSocket (browsers can't send auth headers there)."""
    await contest_service.get(db, user, contest_id)
    return {"ticket": create_ws_ticket(user.id, contest_id), "expires_in": get_settings().ws_ticket_ttl_seconds}


@router.websocket("/{contest_id}/live")
async def live_leaderboard(websocket: WebSocket, contest_id: uuid.UUID, ticket: str = ""):
    """Pushes the top 10 plus your own rank whenever anyone submits (Redis pub/sub); pings every 20s."""
    try:
        user_id = decode_ws_ticket(ticket, contest_id)
    except (jwt.PyJWTError, ValueError):
        await websocket.close(code=4401)
        return
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        try:
            if user is None or not user.is_active:
                raise NotFoundError("User not found")
            await contest_service.get(db, user, contest_id)
        except NotFoundError:
            await websocket.close(code=4404)
            return
    await websocket.accept()

    async def push() -> None:
        async with get_sessionmaker()() as session:
            await websocket.send_json(await live_service.snapshot(session, contest_id, user))

    redis = get_redis()
    pubsub = None
    try:
        await push()
        if redis is None:
            while True:
                await asyncio.sleep(5)
                await push()
        pubsub = redis.pubsub()
        await pubsub.subscribe(live_service.keys(contest_id)[2])
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
            if message is not None:
                await push()
            else:
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if pubsub is not None:
            await pubsub.unsubscribe()
            await pubsub.aclose()


@router.get("/{contest_id}/leaderboard", response_model=Leaderboard)
async def contest_leaderboard(contest_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Ranked by score, then time taken. EduHub-wide boards show first name + initial and school."""
    return await contest_service.leaderboard(db, user, contest_id)


@router.get("/{contest_id}/review", response_model=ContestReview)
async def review_contest(contest_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Correct answers and explanations, available once the contest has ended."""
    return await contest_service.review(db, user, contest_id)


leaderboard_router = APIRouter(prefix="/leaderboard", tags=["contests"])


@leaderboard_router.get("/weekly", response_model=WeeklyLeaderboard)
async def weekly(user: CurrentUser, db: DbSession):
    """Top students in your school by correct answers over the last 7 days."""
    return await classroom_service.weekly_leaderboard(db, user)


classroom_router = APIRouter(prefix="/classroom", tags=["classroom"])


@classroom_router.get("/overview", response_model=ClassOverview)
async def class_overview(user: StaffUser, db: DbSession):
    """Every student's progress plus the topics the class struggles with most."""
    return await classroom_service.overview(db, user)


@classroom_router.get("/students/{student_id}", response_model=StudentDetail)
async def student(student_id: uuid.UUID, user: StaffUser, db: DbSession):
    return await classroom_service.student_detail(db, user, student_id)
