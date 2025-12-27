import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.ai import explain as explain_service
from app.ai.llm import LLMClient, get_llm
from app.api.deps import CurrentUser, DbSession, Pagination, StaffUser
from app.models.quiz import Difficulty
from app.models.user import Role
from app.schemas.paper import AnswerReview, Explanation
from app.schemas.quiz import DisputedQuestion, DuplicateCheck, QuestionAuthor, QuestionCreate, QuestionPublic, SearchHit
from app.services import quiz_service, semantic_service
from app.services.question_filters import Scope, student_scope

router = APIRouter(prefix="/questions", tags=["questions"])


@router.post("", response_model=QuestionAuthor, status_code=status.HTTP_201_CREATED)
async def create_question(data: QuestionCreate, user: StaffUser, db: DbSession):
    return await quiz_service.create_question(db, data, user)


@router.get("", response_model=list[QuestionPublic])
async def list_questions(
    user: CurrentUser,
    db: DbSession,
    page: Pagination,
    subject: str | None = None,
    topic: str | None = None,
    difficulty: Difficulty | None = None,
    grade: Annotated[int | None, Query(ge=1, le=12)] = None,
    scope: Scope = "all",
):
    """Your school's questions plus the shared EduHub library (`scope` narrows to one).
    Students only see their stream's subjects."""
    return await quiz_service.list_questions(
        db,
        user.org_id,
        subject,
        topic,
        difficulty,
        page.limit,
        page.offset,
        grade=grade,
        scope=scope,
        **student_scope(user),
    )


@router.get("/search", response_model=list[SearchHit])
async def search_questions(
    user: CurrentUser,
    db: DbSession,
    q: Annotated[str, Query(min_length=2, max_length=300)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    scope: Scope = "all",
):
    """Semantic search (pgvector, cosine similarity). Students only see their stream and never live-contest questions."""
    return [SearchHit(question=QuestionPublic.model_validate(x), similarity=s) for x, s in await semantic_service.search(db, user, q, limit, scope)]


@router.post("/duplicates", response_model=list[SearchHit])
async def check_duplicates(data: DuplicateCheck, user: StaffUser, db: DbSession):
    """Existing questions that look like the draft (similarity >= 0.8), shown before saving."""
    return [SearchHit(question=QuestionPublic.model_validate(x), similarity=s) for x, s in await semantic_service.near_duplicates(db, user, data.text)]


@router.get("/disputed", response_model=list[DisputedQuestion])
async def disputed_questions(user: StaffUser, db: DbSession):
    """Questions where the AI reviewer is confident the answer key is wrong, awaiting a staff decision."""
    return [
        DisputedQuestion(question=QuestionAuthor.model_validate(q), ai_suggested_choice_id=q.ai_suggested_choice_id, ai_explanation=q.ai_explanation)
        for q in await explain_service.disputed(db, user)
    ]


@router.post("/{question_id}/explain", response_model=Explanation)
async def explain_question(question_id: uuid.UUID, user: CurrentUser, db: DbSession, llm: Annotated[LLMClient, Depends(get_llm)]):
    """The teacher's explanation, or an AI one generated once and cached. Students must have answered it first."""
    return await explain_service.explain(db, llm, user, question_id)


@router.post("/{question_id}/answer-review", response_model=QuestionAuthor)
async def review_answer(question_id: uuid.UUID, data: AnswerReview, user: StaffUser, db: DbSession):
    """Resolve an AI dispute: keep the official key, or accept the AI's suggested answer."""
    return QuestionAuthor.model_validate(await explain_service.review_answer(db, user, question_id, data.action))


@router.get("/{question_id}/similar", response_model=list[SearchHit])
async def similar_questions(
    question_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: Annotated[int, Query(ge=1, le=20)] = 5
):
    return [SearchHit(question=QuestionPublic.model_validate(x), similarity=s) for x, s in await semantic_service.similar_to(db, user, question_id, limit)]


@router.get("/{question_id}", response_model=QuestionAuthor | QuestionPublic)
async def get_question(question_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Teachers and admins also see the answer key and explanation."""
    question = await quiz_service.get_question(db, user.org_id, question_id)
    if user.role in (Role.teacher, Role.admin):
        return QuestionAuthor.model_validate(question)
    return QuestionPublic.model_validate(question)
