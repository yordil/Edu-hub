import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, Pagination
from app.schemas.quiz import AttemptCreate, AttemptDetail, AttemptProgress, AttemptSubmit, AttemptSummary
from app.services import quiz_service

router = APIRouter(prefix="/attempts", tags=["attempts"])


@router.post("", response_model=AttemptDetail, status_code=status.HTTP_201_CREATED)
async def start_attempt(data: AttemptCreate, user: CurrentUser, db: DbSession):
    """Draw a random set of questions; answers stay on the server until submission."""
    return quiz_service.to_detail(await quiz_service.start_attempt(db, data, user))


@router.put("/{attempt_id}/progress", response_model=AttemptDetail)
async def save_progress(attempt_id: uuid.UUID, data: AttemptProgress, user: CurrentUser, db: DbSession):
    """Autosave the answer sheet (answers + flags). Rejected with 409 once submitted or past an exam's deadline."""
    return quiz_service.to_detail(await quiz_service.save_progress(db, attempt_id, data, user))


@router.post("/{attempt_id}/submit", response_model=AttemptDetail)
async def submit_attempt(attempt_id: uuid.UUID, data: AttemptSubmit, user: CurrentUser, db: DbSession):
    return quiz_service.to_detail(await quiz_service.submit_attempt(db, attempt_id, data, user))


@router.get("", response_model=list[AttemptSummary])
async def list_attempts(user: CurrentUser, db: DbSession, page: Pagination):
    return [quiz_service.to_summary(a) for a in await quiz_service.list_attempts(db, user, page.limit, page.offset)]


@router.get("/{attempt_id}", response_model=AttemptDetail)
async def get_attempt(attempt_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return quiz_service.to_detail(await quiz_service.get_attempt(db, attempt_id, user))
