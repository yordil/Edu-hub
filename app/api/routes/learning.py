from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.curriculum import STREAM_SUBJECTS, SUBJECTS
from app.schemas.quiz import Activity, MasteryOut, NextQuestion
from app.services import activity_service, learning_service

router = APIRouter(prefix="/learning", tags=["learning"])


@router.get("/curriculum")
async def curriculum() -> dict:
    """Subjects and which ones each stream studies (public; used to build forms)."""
    return {
        "subjects": list(SUBJECTS),
        "streams": {stream.value: [s for s in SUBJECTS if s in subjects] for stream, subjects in STREAM_SUBJECTS.items()},
    }


@router.get("/activity", response_model=Activity)
async def my_activity(user: CurrentUser, db: DbSession, days: Annotated[int, Query(ge=7, le=371)] = 365):
    """Questions answered per day plus current and longest streak (UTC days)."""
    return await activity_service.activity(db, user, days)


@router.get("/mastery", response_model=list[MasteryOut])
async def my_mastery(user: CurrentUser, db: DbSession):
    """Estimated mastery per topic from the adaptive model, weakest first."""
    return await learning_service.list_mastery(db, user)


@router.get("/next-question", response_model=NextQuestion)
async def next_question(
    user: CurrentUser,
    db: DbSession,
    subject: str | None = None,
    topic: str | None = None,
    grade: Annotated[int | None, Query(ge=1, le=12)] = None,
):
    """The single best question to practice next: ~70% predicted success, weak topics first, fresh over seen."""
    return await learning_service.next_question(db, user, subject, topic, grade)
