import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.ai import tutor
from app.ai.llm import LLMClient, get_llm
from app.api.deps import CurrentUser, DbSession, Pagination, StaffUser, TutorUser
from app.schemas.tutor import (
    ConversationDetail,
    ConversationSummary,
    MemoryOut,
    SchoolAIUsage,
    TutorAccess,
    TutorChatIn,
    TutorChatOut,
)

router = APIRouter(prefix="/tutor", tags=["ai tutor"])


@router.post("/chat", response_model=TutorChatOut)
async def chat(data: TutorChatIn, user: TutorUser, db: DbSession, llm: Annotated[LLMClient, Depends(get_llm)]):
    """Ask the tutor. It looks up the student's mastery, mistakes and recommendations before answering.
    Every model call is metered against the school's monthly token budget."""
    return await tutor.chat(db, llm, user, data)


@router.post("/chat/stream")
async def chat_stream(data: TutorChatIn, user: TutorUser, db: DbSession, llm: Annotated[LLMClient, Depends(get_llm)]):
    """The same turn as /chat, as server-sent events: `status` while the tutor thinks or looks things up,
    `delta` text as it is written, `reset`/`replace` corrections, then `done` (the saved reply, exactly as
    /chat returns it) or `error`. Refusals — plan, budget, rate limit, unknown conversation — are ordinary
    HTTP errors, raised before the stream opens."""
    await tutor.check_turn(db, user, data)
    return StreamingResponse(
        tutor.stream_turn(llm, user.id, data),
        media_type="text/event-stream",
        # no-transform stops proxies compressing (and so buffering) the stream; X-Accel-Buffering does the same for nginx.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/access", response_model=TutorAccess)
async def access(user: CurrentUser, db: DbSession):
    """Whether this school can use the tutor right now: a paid plan, or what's left of the free trial."""
    return await tutor.tutor_access(db, user.org_id)


@router.get("/conversations", response_model=list[ConversationSummary])
async def conversations(user: CurrentUser, db: DbSession, page: Pagination):
    return await tutor.list_conversations(db, user, page.limit, page.offset)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession):
    return await tutor.get_conversation(db, user, conversation_id)


@router.get("/memories", response_model=list[MemoryOut])
async def memories(user: CurrentUser, db: DbSession):
    """What the tutor remembers about you across conversations (newest first)."""
    return [MemoryOut.model_validate(m) for m in await tutor.load_memories(db, user)]


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget(memory_id: uuid.UUID, user: CurrentUser, db: DbSession):
    await tutor.delete_memory(db, user, memory_id)


@router.delete("/memories", status_code=status.HTTP_204_NO_CONTENT)
async def forget_all(user: CurrentUser, db: DbSession):
    await tutor.clear_memories(db, user)


@router.get("/usage", response_model=SchoolAIUsage)
async def usage(user: StaffUser, db: DbSession):
    """This school's AI token usage for the current calendar month (UTC) against its budget."""
    return await tutor.school_usage(db, user.org_id)
