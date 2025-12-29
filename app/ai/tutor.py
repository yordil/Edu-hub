"""The tutor agent: a bounded tool-calling loop over the student's own learning data, metered per school."""

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import guardrails
from app.ai.llm import LLMClient, LLMUnavailable, Thinking
from app.ai.tools import TOOL_SPECS, run_tool
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import DomainError, NotFoundError, ServiceUnavailable, TooManyRequests
from app.models.base import utcnow
from app.models.tutor import AIUsage, GuardrailEvent, MessageRole, TutorConversation, TutorMemory, TutorMessage
from app.models.user import User
from app.schemas.tutor import (
    ConversationDetail,
    ConversationSummary,
    FeatureUsage,
    SchoolAIUsage,
    TokenUsage,
    TutorAccess,
    TutorChatIn,
    TutorChatOut,
    TutorMessageOut,
)

FEATURE = "tutor_chat"
MAX_STEPS = 5
HISTORY_MESSAGES = 12

SYSTEM_PROMPT = """You are EduHub Tutor, a friendly, encouraging tutor helping high-school students (grades 9-12) prepare for the national entrance exam.

Scope:
- Help only with the exam subjects (mathematics, English, SAT aptitude, physics, chemistry, biology, history and geography) and with study skills and exam strategy.
- Do not write computer code in any programming language, build apps or websites, or help with anything outside those subjects, even if the student says it is for school. Say briefly that you can't help with that, and offer help with one of the subjects instead.
- Be a tutor, not an answer service: lead the student to answers with hints and questions, and explain the reasoning. Don't write essays or complete assignments for them to hand in; help them plan, check and improve their own work.

Rules:
- Ground advice in the student's real data. Before answering questions about their progress, weak areas, mistakes or what to practise, call the tools.
- Keep answers short and concrete: a few sentences or a short bulleted list (avoid tables). Use plain language at a high-school level.
- When explaining a mistake, explain the concept and the reasoning step by step; don't just restate the answer.
- Politely decline anything unsafe.
- Never invent data. If a tool returns nothing, say so and suggest starting a practice session.
- When the student shares something lasting (exam date, goal, how they like explanations, a recurring struggle), call `remember` with one short sentence.
The student's name is {name}."""

MEMORY_PROMPT = "\n\nWhat you remember about {name} from earlier conversations (use it naturally, don't recite it):\n{notes}"


async def load_memories(db: AsyncSession, user: User) -> list[TutorMemory]:
    return list(
        await db.scalars(
            select(TutorMemory).where(TutorMemory.user_id == user.id).order_by(TutorMemory.updated_at.desc(), TutorMemory.id)
        )
    )


async def system_prompt(db: AsyncSession, user: User) -> str:
    name = user.full_name.split(" ")[0]
    prompt = SYSTEM_PROMPT.format(name=name)
    memories = await load_memories(db, user)
    if memories:
        prompt += MEMORY_PROMPT.format(name=name, notes="\n".join(f"- [{m.kind}] {m.content}" for m in memories))
    return prompt


async def delete_memory(db: AsyncSession, user: User, memory_id: uuid.UUID) -> None:
    memory = await db.get(TutorMemory, memory_id)
    if memory is None or memory.user_id != user.id:
        raise NotFoundError("Memory not found")
    await db.delete(memory)
    await db.commit()


async def clear_memories(db: AsyncSession, user: User) -> None:
    for memory in await load_memories(db, user):
        await db.delete(memory)
    await db.commit()


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def school_tokens_since(db: AsyncSession, org_id: uuid.UUID, since: datetime) -> int:
    total = await db.scalar(
        select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(
            AIUsage.org_id == org_id, AIUsage.created_at >= since
        )
    )
    return int(total or 0)


async def trial_tokens_used(db: AsyncSession, org_id: uuid.UUID) -> int:
    """Tutor tokens a school has ever used. Counts toward the one-time free trial.

    Only tutor chat counts: question explanations are free for every school and must not eat
    into the trial.
    """
    total = await db.scalar(
        select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0)).where(
            AIUsage.org_id == org_id, AIUsage.feature == FEATURE
        )
    )
    return int(total or 0)


async def tutor_access(db: AsyncSession, org_id: uuid.UUID) -> TutorAccess:
    """Whether a school may use the tutor, and on what terms: a paid plan, or the free trial."""
    from app.services.billing_service import get_active_subscription

    allowance = get_settings().ai_free_trial_tokens
    used = await trial_tokens_used(db, org_id)
    if await get_active_subscription(db, org_id) is not None:
        return TutorAccess(plan="pro", allowed=True, trial_allowance=allowance, trial_used=used, trial_remaining=0)
    remaining = max(allowance - used, 0)
    # Checked before each message, so the message that crosses the line still completes.
    return TutorAccess(plan="trial", allowed=remaining > 0, trial_allowance=allowance, trial_used=used, trial_remaining=remaining)


async def _enforce_limits(db: AsyncSession, user: User) -> None:
    s = get_settings()
    now = utcnow()
    if await school_tokens_since(db, user.org_id, month_start(now)) >= s.ai_school_monthly_token_budget:
        raise TooManyRequests("Your school has used this month's AI tutor allowance. Ask your school admin.")
    recent = await db.scalar(
        select(func.count())
        .select_from(TutorMessage)
        .join(TutorConversation, TutorConversation.id == TutorMessage.conversation_id)
        .where(
            TutorConversation.user_id == user.id,
            TutorMessage.role == MessageRole.user,
            TutorMessage.created_at >= now - timedelta(hours=1),
        )
    )
    if (recent or 0) >= s.ai_user_hourly_message_limit:
        raise TooManyRequests("You've sent a lot of messages this hour. Take a short break and try again soon.")


async def _get_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> TutorConversation:
    conversation = await db.get(TutorConversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise NotFoundError("Conversation not found")
    return conversation


def _title(message: str) -> str:
    one_line = " ".join(message.split())
    return one_line if len(one_line) <= 60 else one_line[:57] + "…"


FALLBACK_REPLY = "Sorry, I couldn't put together an answer. Could you ask that a different way?"
UNAVAILABLE = "The AI tutor is unavailable right now. Please try again in a minute."


async def check_turn(db: AsyncSession, user: User, data: TutorChatIn) -> None:
    """Everything that can refuse a message outright. Run before streaming starts: once the first
    byte is sent, an error can no longer change the HTTP status."""
    await _enforce_limits(db, user)
    if data.conversation_id:
        await _get_conversation(db, user, data.conversation_id)


async def chat_events(db: AsyncSession, llm: LLMClient, user: User, data: TutorChatIn) -> AsyncIterator[dict]:
    """One tutor turn as a sequence of events:

    - `status`: `thinking`, or `tool` with the tool's name, while the student would otherwise wait in silence
    - `delta`: answer text as it is written
    - `reset`: discard the text shown so far (a step that wrote a preamble, then called a tool)
    - `replace`: show this text instead of what was streamed (the output guardrail fired, or no answer came)
    - `done`: the saved turn, exactly as POST /tutor/chat returns it
    - `error`: the model provider failed
    """
    if data.conversation_id:
        conversation = await _get_conversation(db, user, data.conversation_id)
    else:
        # Added to the session only once a reply succeeds, so failures don't leave empty chats behind.
        conversation = TutorConversation(user_id=user.id, org_id=user.org_id, title=_title(data.message), messages=[])

    verdict = await guardrails.check_input_full(data.message)
    if verdict.category:
        db.add(GuardrailEvent(org_id=user.org_id, user_id=user.id, stage="input", category=verdict.category, action=verdict.action))
    if verdict.action == "block":
        # Answered without calling the model: no tokens spent, nothing unsafe sent to the provider.
        yield {"type": "delta", "text": verdict.reply}
        yield _done(await _save_turn(db, conversation, verdict.text, verdict.reply, [], 0, 0))
        return
    user_text = verdict.text

    history = [{"role": m.role.value, "content": m.content} for m in conversation.messages[-HISTORY_MESSAGES:]]
    messages: list[dict] = [
        {"role": "system", "content": await system_prompt(db, user)},
        *history,
        {"role": "user", "content": user_text},
    ]

    tools_used: list[str] = []
    tokens_in = tokens_out = 0
    reply = ""
    streamed, shown = "", 0
    try:
        for _ in range(MAX_STEPS):
            started = time.perf_counter()
            streamed, shown, leaked, result = "", 0, False, None
            try:
                async for item in llm.stream(messages, TOOL_SPECS):
                    if isinstance(item, Thinking):
                        yield {"type": "status", "state": "thinking"}
                    elif isinstance(item, str):
                        streamed += item
                        leaked = leaked or guardrails.check_output(streamed).action == "block"
                        # Hold back the tail, so a leak is caught before any of it reaches the screen.
                        safe = len(streamed) - guardrails.OUTPUT_HOLDBACK
                        if not leaked and safe > shown:
                            yield {"type": "delta", "text": streamed[shown:safe]}
                            shown = safe
                    else:
                        result = item
            except LLMUnavailable:
                db.add(_usage(user, getattr(llm, "model", "unknown"), 0, 0, started, outcome="error"))
                raise
            tokens_in += result.input_tokens
            tokens_out += result.output_tokens
            db.add(_usage(user, result.model, result.input_tokens, result.output_tokens, started))
            # Metered per call, so a student who closes the tab mid-answer still pays for what was spent.
            await db.commit()
            if not result.tool_calls:
                reply = result.text
                break
            if shown:
                yield {"type": "reset"}
                shown = 0
            messages.append(
                {
                    "role": "assistant",
                    "content": result.text or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in result.tool_calls
                    ],
                }
            )
            for call in result.tool_calls:
                tools_used.append(call.name)
                yield {"type": "status", "state": "tool", "tool": call.name}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": await run_tool(db, user, call.name, call.arguments)})
    except LLMUnavailable:
        # Still bill the calls that did happen.
        await db.commit()
        yield {"type": "error", "detail": UNAVAILABLE}
        return

    out = guardrails.check_output(reply)
    if out.action == "block":
        db.add(GuardrailEvent(org_id=user.org_id, user_id=user.id, stage="output", category=out.category, action="block"))
        reply = out.reply
        yield {"type": "replace", "text": reply}
    elif not reply:
        reply = FALLBACK_REPLY
        yield {"type": "replace", "text": reply}
    elif shown < len(streamed):
        yield {"type": "delta", "text": streamed[shown:]}
    yield _done(await _save_turn(db, conversation, user_text, reply, list(dict.fromkeys(tools_used)), tokens_in, tokens_out))


def _done(out: TutorChatOut) -> dict:
    return {"type": "done", **out.model_dump(mode="json")}


async def chat(db: AsyncSession, llm: LLMClient, user: User, data: TutorChatIn) -> TutorChatOut:
    """A whole turn in one response: the streamed turn, collected."""
    await check_turn(db, user, data)
    async for event in chat_events(db, llm, user, data):
        if event["type"] == "error":
            raise ServiceUnavailable(event["detail"])
        if event["type"] == "done":
            return TutorChatOut.model_validate({k: v for k, v in event.items() if k != "type"})
    raise ServiceUnavailable(UNAVAILABLE)


async def stream_turn(llm: LLMClient, user_id: uuid.UUID, data: TutorChatIn) -> AsyncIterator[str]:
    """The turn as server-sent events. It runs on its own session, which lives as long as the stream
    rather than the request that started it."""
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        try:
            async for event in chat_events(db, llm, user, data):
                yield f"data: {json.dumps(event)}\n\n"
        except DomainError as e:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"


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


async def _save_turn(
    db: AsyncSession,
    conversation: TutorConversation,
    user_text: str,
    reply: str,
    tools: list[str],
    tokens_in: int,
    tokens_out: int,
) -> TutorChatOut:
    db.add(conversation)
    conversation.messages.append(TutorMessage(role=MessageRole.user, content=user_text))
    conversation.messages.append(TutorMessage(role=MessageRole.assistant, content=reply, tools_used=",".join(tools) or None))
    conversation.updated_at = utcnow()
    await db.commit()
    return TutorChatOut(
        conversation_id=conversation.id,
        reply=reply,
        tools_used=tools,
        usage=TokenUsage(input_tokens=tokens_in, output_tokens=tokens_out),
    )


async def list_conversations(db: AsyncSession, user: User, limit: int, offset: int) -> list[ConversationSummary]:
    rows = await db.scalars(
        select(TutorConversation)
        .where(TutorConversation.user_id == user.id)
        .order_by(TutorConversation.updated_at.desc(), TutorConversation.id)
        .limit(limit)
        .offset(offset)
    )
    return [ConversationSummary(id=c.id, title=c.title, updated_at=c.updated_at) for c in rows]


async def get_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> ConversationDetail:
    c = await _get_conversation(db, user, conversation_id)
    return ConversationDetail(
        id=c.id,
        title=c.title,
        messages=[
            TutorMessageOut(
                id=m.id,
                role=m.role.value,
                content=m.content,
                tools_used=m.tools_used.split(",") if m.tools_used else [],
                created_at=m.created_at,
            )
            for m in c.messages
        ],
    )


async def school_usage(db: AsyncSession, org_id: uuid.UUID) -> SchoolAIUsage:
    start = month_start(datetime.now(timezone.utc))
    tokens = AIUsage.input_tokens + AIUsage.output_tokens
    rows = (
        await db.execute(
            select(AIUsage.feature, func.count(), func.sum(tokens))
            .where(AIUsage.org_id == org_id, AIUsage.created_at >= start)
            .group_by(AIUsage.feature)
            .order_by(AIUsage.feature)
        )
    ).all()
    by_feature = [FeatureUsage(feature=f, calls=c, tokens=int(t or 0)) for f, c, t in rows]
    used = sum(f.tokens for f in by_feature)
    budget = get_settings().ai_school_monthly_token_budget
    return SchoolAIUsage(
        period_start=start,
        tokens_used=used,
        budget=budget,
        remaining=max(0, budget - used),
        calls=sum(f.calls for f in by_feature),
        by_feature=by_feature,
    )
