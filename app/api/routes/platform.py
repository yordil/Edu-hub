from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import DbSession, PlatformAdmin
from app.schemas.contest import ContestCreate, ContestOut
from app.schemas.platform import AIMetrics, AnnouncementCreate, AnnouncementOut, PlatformMetrics
from app.schemas.quiz import ImportResult, QuestionAuthor, QuestionCreate, QuestionImport
from app.services import announcement_service, contest_service, platform_service, quiz_service, semantic_service

router = APIRouter(prefix="/platform", tags=["platform"])


@router.get("/metrics", response_model=PlatformMetrics)
async def metrics(_: PlatformAdmin, db: DbSession):
    """Cross-school KPIs, 30-day activity, per-school breakdown and subject performance."""
    return await platform_service.metrics(db)


@router.get("/ai-metrics", response_model=AIMetrics)
async def ai_metrics(_: PlatformAdmin, db: DbSession, days: Annotated[int, Query(ge=1, le=90)] = 7):
    """AI observability: call volume, error rate, latency percentiles, tokens and guardrail interventions."""
    return await platform_service.ai_metrics(db, days)


@router.post("/questions", response_model=QuestionAuthor, status_code=status.HTTP_201_CREATED)
async def create_library_question(data: QuestionCreate, admin: PlatformAdmin, db: DbSession):
    """Add one question to the shared library every school can practice from."""
    (question,) = await quiz_service.create_library_questions(db, [data], admin)
    return question


@router.post("/questions/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
async def import_library_questions(data: QuestionImport, admin: PlatformAdmin, db: DbSession):
    """Bulk-load up to 500 library questions; the whole batch is rejected if any item is invalid."""
    created = await quiz_service.create_library_questions(db, data.questions, admin)
    duplicates = await semantic_service.count_library_duplicates(db, [q.id for q in created])
    return ImportResult(created=len(created), possible_duplicates=duplicates)


@router.post("/announcements", response_model=AnnouncementOut, status_code=status.HTTP_201_CREATED)
async def create_platform_announcement(data: AnnouncementCreate, admin: PlatformAdmin, db: DbSession):
    """Broadcast to every school."""
    return announcement_service.to_out(await announcement_service.create(db, admin, data, platform_wide=True))


@router.post("/contests", response_model=ContestOut, status_code=status.HTTP_201_CREATED)
async def create_platform_contest(data: ContestCreate, admin: PlatformAdmin, db: DbSession):
    """An EduHub-wide contest drawn from the shared library and open to every school."""
    contest = await contest_service.create(db, admin, data, platform_wide=True)
    return await contest_service.get(db, admin, contest.id)
