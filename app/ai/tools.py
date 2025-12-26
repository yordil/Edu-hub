"""Tools the tutor model can call. Every tool is scoped to the signed-in student; arguments come from the model,
so they are validated and clamped here rather than trusted."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.base import utcnow
from app.models.quiz import Attempt, AttemptItem, Choice
from app.models.user import User
from app.services import learning_service


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> str | None:
    return value.strip()[:120] if isinstance(value, str) and value.strip() else None


async def get_mastery(db: AsyncSession, user: User, args: dict) -> dict:
    items = await learning_service.list_mastery(db, user)
    return {
        "topics": [
            {
                "subject": m.subject,
                "topic": m.topic,
                "mastery_percent": round(m.mastery * 100),
                "level": m.level,
                "answered": m.answered,
                "correct": m.correct,
            }
            for m in items[: _int(args.get("limit"), 15, 1, 30)]
        ],
        "order": "weakest first",
    }


async def get_recent_mistakes(db: AsyncSession, user: User, args: dict) -> dict:
    """Only from submitted attempts, so the tutor can never leak answers to a quiz in progress."""
    limit = _int(args.get("limit"), 5, 1, 10)
    stmt = (
        select(AttemptItem)
        .join(Attempt, Attempt.id == AttemptItem.attempt_id)
        .where(Attempt.user_id == user.id, Attempt.submitted_at.is_not(None), AttemptItem.is_correct.is_(False))
        .order_by(Attempt.submitted_at.desc(), AttemptItem.position)
        .limit(limit)
    )
    items = list(await db.scalars(stmt))
    chosen_ids = [i.selected_choice_id for i in items if i.selected_choice_id]
    chosen = {c.id: c.text for c in await db.scalars(select(Choice).where(Choice.id.in_(chosen_ids)))} if chosen_ids else {}
    topic = _str(args.get("topic"))
    return {
        "mistakes": [
            {
                "subject": i.question.subject,
                "topic": i.question.topic,
                "question": i.question.prompt,
                "student_answer": chosen.get(i.selected_choice_id, "(skipped)"),
                "correct_answer": i.question.correct_choice.text,
                "explanation": i.question.explanation,
            }
            for i in items
            if not topic or i.question.topic == topic.lower()
        ]
    }


async def recommend_questions(db: AsyncSession, user: User, args: dict) -> dict:
    """Practice questions picked by the adaptive model. Answers are deliberately omitted."""
    ranked = await learning_service.rank_questions(db, user, _str(args.get("subject")), _str(args.get("topic")))
    picks = ranked[: _int(args.get("limit"), 3, 1, 5)]
    questions = await learning_service.load_questions_in_order(db, [r.candidate.id for r in picks])
    return {
        "questions": [
            {
                "subject": q.subject,
                "topic": q.topic,
                "difficulty": q.difficulty.value,
                "question": q.prompt,
                "predicted_success_percent": round(r.p * 100),
            }
            for q, r in zip(questions, picks)
        ]
    }


async def find_study_material(db: AsyncSession, user: User, args: dict) -> dict:
    """RAG over the question bank. Full explanations only for questions this student already answered (so it can't
    leak answers to unseen practice or contest questions); unseen matches come back as practice suggestions."""
    from app.services import semantic_service

    query = _str(args.get("query")) or ""
    if not query:
        return {"error": "query is required"}
    limit = _int(args.get("limit"), 3, 1, 5)
    seen = set(
        await db.scalars(
            select(AttemptItem.question_id)
            .join(Attempt, Attempt.id == AttemptItem.attempt_id)
            .where(Attempt.user_id == user.id, Attempt.submitted_at.is_not(None))
        )
    )
    vector = (await semantic_service.get_embedder().embed([query]))[0]
    reviewed = await semantic_service._search_vector(db, user, vector, limit, only=seen) if seen else []
    fresh = await semantic_service._search_vector(db, user, vector, limit, exclude=seen)
    return {
        "from_your_history": [
            {
                "topic": q.topic,
                "question": q.prompt,
                "correct_answer": q.correct_choice.text,
                "explanation": q.explanation,
                "relevance": s,
            }
            for q, s in reviewed
        ],
        "new_practice_questions": [{"topic": q.topic, "question": q.prompt, "relevance": s} for q, s in fresh],
    }


ToolFn = Callable[[AsyncSession, User, dict], Awaitable[dict]]

MEMORY_KINDS = ("goal", "preference", "struggle", "fact")
MAX_MEMORIES = 20


async def remember(db: AsyncSession, user: User, args: dict) -> dict:
    """Long-term memory across conversations. Notes pass the same guardrails as chat input (PII is redacted)."""
    from app.ai import guardrails
    from app.models.tutor import TutorMemory

    note = args.get("note")
    if not isinstance(note, str) or not note.strip():
        return {"error": "note is required"}
    verdict = guardrails.check_input(" ".join(note.split())[:300])
    if verdict.action == "block":
        return {"saved": False, "reason": "not something to remember"}
    kind = args.get("kind") if args.get("kind") in MEMORY_KINDS else "fact"
    content = verdict.text

    existing = list(await db.scalars(select(TutorMemory).where(TutorMemory.user_id == user.id).order_by(TutorMemory.updated_at.desc())))
    same = next((m for m in existing if m.content.lower() == content.lower()), None)
    if same:
        same.kind = kind
        same.updated_at = utcnow()
    else:
        db.add(TutorMemory(user_id=user.id, org_id=user.org_id, kind=kind, content=content))
        for old in existing[MAX_MEMORIES - 1 :]:
            await db.delete(old)
    await db.flush()
    return {"saved": True, "kind": kind, "note": content}


TOOLS: dict[str, ToolFn] = {
    "get_mastery": get_mastery,
    "get_recent_mistakes": get_recent_mistakes,
    "recommend_questions": recommend_questions,
    "find_study_material": find_study_material,
    "remember": remember,
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_mastery",
            "description": "The student's estimated mastery per topic, weakest first. Use for 'what should I focus on' or progress questions.",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max topics (default 15)"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_mistakes",
            "description": "Questions the student recently got wrong, with their answer, the correct answer and an explanation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max mistakes (default 5)"},
                    "topic": {"type": "string", "description": "Optional topic filter, e.g. 'genetics'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_questions",
            "description": "Practice questions chosen by the adaptive engine for this student (no answers included).",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "topic": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max questions (default 3)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_study_material",
            "description": "Semantic search for study material on a concept (e.g. 'balancing chemical equations'): explanations "
            "from questions the student already answered, plus related new practice questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The concept or question to look up"},
                    "limit": {"type": "integer", "description": "Max results per group (default 3)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a short, lasting note about the student to recall in future conversations: an exam date or goal, "
            "how they like explanations, or a recurring struggle. Not for one-off questions or personal contact details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "One sentence, e.g. 'National exam on June 12; aiming for 85%+'"},
                    "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                },
                "required": ["note"],
            },
        },
    },
]


async def run_tool(db: AsyncSession, user: User, name: str, args: dict) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool {name}"})
    try:
        return json.dumps(await fn(db, user, args), ensure_ascii=False)
    except Exception as e:  # A failing tool is reported to the model instead of failing the whole chat.
        return json.dumps({"error": f"{name} failed: {type(e).__name__}"})
