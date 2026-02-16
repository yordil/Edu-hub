"""Semantic search over the question bank with pgvector: similar questions, duplicate detection and RAG material."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingUnavailable, get_embedder
from app.core.errors import NotFoundError, ServiceUnavailable
from app.models.base import utcnow
from app.models.contest import Contest, ContestQuestion
from app.models.embedding import QuestionEmbedding
from app.models.quiz import Question
from app.models.user import Role, User
from app.services.question_filters import Scope, filter_questions, is_visible, student_scope

log = logging.getLogger("eduhub.semantic")

DUPLICATE_THRESHOLD = 0.92
NEAR_DUPLICATE_THRESHOLD = 0.8


def question_text(q: Question) -> str:
    correct = next((c.text for c in q.choices if c.is_correct), "")
    return f"{q.subject}. {q.topic}. {q.prompt} Answer: {correct}. {q.explanation or ''}".strip()


async def embed_questions(db: AsyncSession, questions: list[Question]) -> int:
    """Best-effort: a failing embedding provider must never block creating questions (backfill catches up)."""
    if not questions:
        return 0
    embedder = get_embedder()
    try:
        vectors = await embedder.embed([question_text(q) for q in questions])
    except EmbeddingUnavailable as e:
        log.warning("embedding skipped", extra={"fields": {"error": str(e), "count": len(questions)}})
        return 0
    stmt = pg_insert(QuestionEmbedding).values(
        [{"question_id": q.id, "model": embedder.model, "embedding": v, "updated_at": utcnow()} for q, v in zip(questions, vectors)]
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[QuestionEmbedding.question_id],
            set_={"model": stmt.excluded.model, "embedding": stmt.excluded.embedding, "updated_at": stmt.excluded.updated_at},
        )
    )
    await db.commit()
    return len(questions)


async def backfill(db: AsyncSession, batch_size: int = 64) -> int:
    """Embed every question that has no vector for the current model."""
    model = get_embedder().model
    total = 0
    while True:
        missing = list(
            await db.scalars(
                select(Question)
                .outerjoin(QuestionEmbedding, (QuestionEmbedding.question_id == Question.id) & (QuestionEmbedding.model == model))
                .where(QuestionEmbedding.question_id.is_(None))
                .limit(batch_size)
            )
        )
        if not missing:
            return total
        done = await embed_questions(db, missing)
        if done == 0:
            return total
        total += done


def _active_contest_questions():
    """Questions in contests that haven't ended: hidden from students so contests can't be previewed."""
    return select(ContestQuestion.question_id).join(Contest, Contest.id == ContestQuestion.contest_id).where(Contest.ends_at > utcnow())


async def _search_vector(
    db: AsyncSession,
    user: User,
    vector: list[float],
    limit: int,
    scope: Scope = "all",
    exclude: set[uuid.UUID] | None = None,
    only: set[uuid.UUID] | None = None,
) -> list[tuple[Question, float]]:
    model = get_embedder().model
    distance = QuestionEmbedding.embedding.cosine_distance(vector)
    stmt = filter_questions(
        select(Question, distance.label("distance")).join(QuestionEmbedding, QuestionEmbedding.question_id == Question.id),
        user.org_id,
        scope=scope,
        **student_scope(user),
    ).where(QuestionEmbedding.model == model)
    if user.role is Role.student:
        stmt = stmt.where(Question.id.not_in(_active_contest_questions()))
    if exclude:
        stmt = stmt.where(Question.id.not_in(exclude))
    if only is not None:
        stmt = stmt.where(Question.id.in_(only))
    rows = await db.execute(stmt.order_by(distance).limit(limit))
    return [(q, round(1 - float(d), 4)) for q, d in rows]


async def _embed_query(text: str) -> list[float]:
    try:
        return (await get_embedder().embed([text]))[0]
    except EmbeddingUnavailable:
        raise ServiceUnavailable("Search is temporarily unavailable") from None


async def search(db: AsyncSession, user: User, query: str, limit: int, scope: Scope = "all") -> list[tuple[Question, float]]:
    return await _search_vector(db, user, await _embed_query(query), limit, scope)


async def similar_to(db: AsyncSession, user: User, question_id: uuid.UUID, limit: int) -> list[tuple[Question, float]]:
    q = await db.get(Question, question_id)
    if q is None or not is_visible(q, user.org_id):
        raise NotFoundError("Question not found")
    row = await db.get(QuestionEmbedding, q.id)
    vector = list(row.embedding) if row and row.model == get_embedder().model else await _embed_query(question_text(q))
    return await _search_vector(db, user, vector, limit, exclude={q.id})


async def near_duplicates(db: AsyncSession, user: User, text: str, limit: int = 3) -> list[tuple[Question, float]]:
    """Retrieve candidates from the index, then re-score draft vs. each candidate's prompt alone
    (stored vectors also embed subject/answer/explanation, which a bare draft doesn't have)."""
    draft = await _embed_query(text)
    candidates = [q for q, _ in await _search_vector(db, user, draft, 10)]
    if not candidates:
        return []
    try:
        vectors = await get_embedder().embed([q.prompt for q in candidates])
    except EmbeddingUnavailable:
        raise ServiceUnavailable("Search is temporarily unavailable") from None
    scored = [(q, round(_cosine(draft, v), 4)) for q, v in zip(candidates, vectors)]
    return sorted([h for h in scored if h[1] >= NEAR_DUPLICATE_THRESHOLD], key=lambda h: -h[1])[:limit]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def count_library_duplicates(db: AsyncSession, new_ids: list[uuid.UUID]) -> int:
    """How many freshly imported library questions closely match a pre-existing library question."""
    model = get_embedder().model
    count = 0
    for qid in new_ids:
        row = await db.get(QuestionEmbedding, qid)
        if row is None or row.model != model:
            continue
        distance = QuestionEmbedding.embedding.cosine_distance(list(row.embedding))
        nearest = await db.scalar(
            select(distance)
            .join(Question, Question.id == QuestionEmbedding.question_id)
            .where(Question.org_id.is_(None), QuestionEmbedding.model == model, Question.id.not_in(new_ids))
            .order_by(distance)
            .limit(1)
        )
        if nearest is not None and 1 - float(nearest) >= DUPLICATE_THRESHOLD:
            count += 1
    return count
