"""On-demand explanations for questions without a teacher-written one, plus an AI second opinion on the answer key.

Generated once per question and cached. The AI never silently overrides the official key: when it is confident another
choice is right, the question is marked "disputed" and staff decide (keep the key or accept the AI's answer)."""

import json
import re
import time
import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import guardrails
from app.ai.llm import LLMClient, LLMUnavailable
from app.ai.tutor import month_start, school_tokens_since
from app.core.config import get_settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ServiceUnavailable, TooManyRequests
from app.models.base import utcnow
from app.models.contest import ContestAnswer, ContestEntry
from app.models.quiz import Attempt, AttemptItem, Question
from app.models.tutor import AIUsage
from app.models.user import Role, User
from app.schemas.paper import Explanation
from app.services.question_filters import is_visible

FEATURE = "explain"
LETTERS = "ABCDEF"

PROMPT = """You are an expert exam reviewer for Ethiopian high-school (grade 9-12) and national entrance exams.
Solve the multiple-choice question yourself first, then compare with the official answer key.

Reply with ONLY a JSON object:
{{"your_answer": "<letter>", "confidence": "high" | "medium" | "low", "explanation": "<explanation>"}}

The explanation is for a student: 3-6 short sentences or bullets, plain language. Explain why the correct option is right
and briefly why the tempting wrong options are wrong. If you believe the official key is wrong, explain the option you
believe is correct and say clearly that the official key may contain an error.

Question: {prompt}
{choices}
Official answer key: {official}"""


async def _student_has_answered(db: AsyncSession, user: User, question_id: uuid.UUID) -> bool:
    """Students only get explanations for questions they already submitted, so this can't leak live answers."""
    in_attempt = exists().where(
        AttemptItem.question_id == question_id,
        AttemptItem.attempt_id == Attempt.id,
        Attempt.user_id == user.id,
        Attempt.submitted_at.is_not(None),
    )
    in_contest = exists().where(
        ContestAnswer.question_id == question_id,
        ContestAnswer.entry_id == ContestEntry.id,
        ContestEntry.user_id == user.id,
        ContestEntry.submitted_at.is_not(None),
    )
    return bool(await db.scalar(select(in_attempt | in_contest)))


def _parse(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _result(q: Question, source: str, text: str) -> Explanation:
    return Explanation(
        question_id=q.id,
        source=source,
        explanation=text,
        official_choice_id=q.correct_choice.id,
        ai_check=q.ai_check,
        ai_suggested_choice_id=q.ai_suggested_choice_id,
    )


async def explain(db: AsyncSession, llm: LLMClient, user: User, question_id: uuid.UUID) -> Explanation:
    q = await db.get(Question, question_id)
    if q is None or not is_visible(q, user.org_id):
        raise NotFoundError("Question not found")
    if user.role is Role.student and not await _student_has_answered(db, user, q.id):
        raise ForbiddenError("Answer this question first, then ask for an explanation")
    if q.explanation:
        return _result(q, "teacher", q.explanation)
    if q.ai_explanation:
        return _result(q, "ai", q.ai_explanation)

    s = get_settings()
    if await school_tokens_since(db, user.org_id, month_start(utcnow())) >= s.ai_school_monthly_token_budget:
        raise TooManyRequests("Your school has used this month's AI allowance. Ask your school admin.")

    official = LETTERS[[c.id for c in q.choices].index(q.correct_choice.id)]
    choices = "\n".join(f"{LETTERS[i]}. {c.text}" for i, c in enumerate(q.choices))
    messages = [{"role": "user", "content": PROMPT.format(prompt=q.prompt, choices=choices, official=official)}]
    started = time.perf_counter()
    try:
        result = await llm.complete(messages, None, max_tokens=900)
    except LLMUnavailable:
        db.add(_usage(user, getattr(llm, "model", "unknown"), 0, 0, started, "error"))
        await db.commit()
        raise ServiceUnavailable("Explanations are unavailable right now. Please try again in a minute.") from None
    db.add(_usage(user, result.model, result.input_tokens, result.output_tokens, started))

    data = _parse(result.text)
    text = str(data.get("explanation") or "").strip() or result.text.strip()
    if guardrails.check_output(text).action == "block" or not text:
        await db.commit()
        raise ServiceUnavailable("Couldn't produce an explanation for this question.")
    letter = str(data.get("your_answer") or "").strip().upper()[:1]
    confidence = str(data.get("confidence") or "").lower()

    # Re-read under lock: another request may have generated it meanwhile.
    q = await db.get(Question, question_id, with_for_update=True, populate_existing=True)
    if q.ai_explanation:
        await db.commit()
        return _result(q, "ai", q.ai_explanation)
    q.ai_explanation = text[:5000]
    # `letter` is "" when the reply wasn't structured, and "" is a substring of every string, so
    # without the truthiness check an unparseable reply would be recorded as an "unsure" verdict.
    if letter and letter in LETTERS[: len(q.choices)]:
        if letter == official:
            q.ai_check = "agrees"
        elif confidence == "high":
            q.ai_check = "disputed"
            q.ai_suggested_choice_id = q.choices[LETTERS.index(letter)].id
        else:
            q.ai_check = "unsure"
    await db.commit()
    return _result(q, "ai", q.ai_explanation)


def _usage(user: User, model: str, tokens_in: int, tokens_out: int, started: float, outcome: str = "ok") -> AIUsage:
    return AIUsage(
        org_id=user.org_id,
        user_id=user.id,
        feature=FEATURE,
        model=model,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=int((time.perf_counter() - started) * 1000),
        outcome=outcome,
    )


def _can_correct(q: Question, user: User) -> bool:
    if q.org_id is None:
        return user.is_platform_admin
    return q.org_id == user.org_id and user.role in (Role.teacher, Role.admin)


async def disputed(db: AsyncSession, user: User) -> list[Question]:
    """Open answer-key disputes this user may resolve (their school's questions; library ones for EduHub admins)."""
    rows = await db.scalars(select(Question).where(Question.ai_check == "disputed").order_by(Question.created_at, Question.id))
    return [q for q in rows if _can_correct(q, user)]


async def review_answer(db: AsyncSession, user: User, question_id: uuid.UUID, action: str) -> Question:
    q = await db.get(Question, question_id, with_for_update=True)
    if q is None or not is_visible(q, user.org_id) or not _can_correct(q, user):
        raise NotFoundError("Question not found")
    if q.ai_check != "disputed":
        raise ConflictError("This question has no open answer-key dispute")
    if action == "accept_ai":
        for c in q.choices:
            c.is_correct = c.id == q.ai_suggested_choice_id
        q.ai_check = "corrected"
    else:
        q.ai_check = "confirmed"
        # The cached text argued for the AI's answer; regenerate it against the confirmed key next time.
        q.ai_explanation = None
    q.ai_suggested_choice_id = None
    await db.commit()
    return q
