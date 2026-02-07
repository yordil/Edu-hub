import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import ValidationError

from app.api.deps import ActivePlanUser, CurrentUser, DbSession, StaffUser
from app.core.config import get_settings
from app.core.errors import ValidationFailed
from app.models.paper import ExamPaper
from app.schemas.paper import ItemUpdate, PaperDetail, PaperItemOut, PaperPatch, PaperSummary, PaperUpload, PublishResult
from app.schemas.quiz import AttemptDetail
from app.services import paper_service, quiz_service

router = APIRouter(prefix="/papers", tags=["past papers"])


def summary(p: ExamPaper) -> PaperSummary:
    return PaperSummary(
        id=p.id,
        scope="library" if p.org_id is None else "school",
        status=p.status,
        title=p.title,
        subject=p.subject,
        grade=p.grade,
        year=p.year,
        time_limit_minutes=p.time_limit_minutes,
        questions=len(p.question_ids) if p.status == "published" else len(p.items),
        created_at=p.created_at,
        published_at=p.published_at,
    )


def detail(p: ExamPaper) -> PaperDetail:
    items = [PaperItemOut(**i, problems=paper_service.item_problems(i)) for i in p.items]
    return PaperDetail(
        **summary(p).model_dump(),
        failure_reason=p.failure_reason,
        filename=p.filename,
        default_topic=p.default_topic,
        page_count=p.page_count,
        scanned=p.scanned,
        ocr_used=p.ocr_used,
        has_key=p.has_key,
        warnings=p.warnings,
        ready=sum(1 for i in items if i.include and not i.problems),
        items=items,
    )


async def _read(upload: UploadFile | None) -> bytes | None:
    if upload is None or not upload.filename:
        return None
    # One byte over the limit is enough for the service to reject it.
    data = await upload.read(get_settings().paper_max_upload_bytes + 1)
    return data or None


@router.post("", response_model=PaperDetail, status_code=status.HTTP_202_ACCEPTED)
async def upload_paper(
    user: StaffUser,
    db: DbSession,
    exam: Annotated[UploadFile, File(description="The question paper (PDF)")],
    answer_key: Annotated[UploadFile | None, File(description="Optional answer key (PDF)")] = None,
    answer_key_text: Annotated[str | None, Form(max_length=20000)] = None,
    title: Annotated[str | None, Form()] = None,
    subject: Annotated[str | None, Form()] = None,
    grade: Annotated[int | None, Form()] = None,
    year: Annotated[str | None, Form()] = None,
    time_limit_minutes: Annotated[int | None, Form()] = None,
    default_topic: Annotated[str | None, Form()] = None,
    library: Annotated[bool, Form()] = False,
):
    """Upload a past paper. Returns 202 with status "queued": extraction and OCR run in a worker,
    so poll this paper until it reaches "review" (or "failed", which carries a reason).

    The answer key can be a PDF, pasted text ("1. B 2. D", "1-B", or "BDBC..."), or an answers
    section inside the paper. Extracted text is screened by the content filter, and nothing is
    published until a human reviews it."""
    try:
        meta = PaperUpload(
            title=title or None, subject=subject or None, grade=grade, year=year or None,
            time_limit_minutes=time_limit_minutes, default_topic=default_topic or None, library=library,
        )
    except ValidationError as e:
        raise ValidationFailed(e.errors()[0]["msg"].replace("Value error, ", "")) from None
    data = await _read(exam)
    if data is None:
        raise ValidationFailed("Choose the exam PDF to upload")
    paper = await paper_service.enqueue_paper(
        db, user, meta, exam.filename or "paper.pdf", data, await _read(answer_key), (answer_key_text or "").strip() or None
    )
    return detail(paper)


@router.get("", response_model=list[PaperSummary])
async def list_papers(user: CurrentUser, db: DbSession):
    """Published past papers you can take (plus drafts you can review, for staff)."""
    return [summary(p) for p in await paper_service.list_papers(db, user)]


@router.get("/{paper_id}", response_model=PaperDetail | PaperSummary)
async def get_paper(paper_id: uuid.UUID, user: CurrentUser, db: DbSession):
    paper = await paper_service.get_paper(db, user, paper_id)
    # Students see the summary only: the review data contains the answer key.
    return detail(paper) if paper_service._can_manage(paper, user) else summary(paper)


@router.patch("/{paper_id}", response_model=PaperDetail)
async def update_paper(paper_id: uuid.UUID, data: PaperPatch, user: StaffUser, db: DbSession):
    return detail(await paper_service.update_paper(db, user, paper_id, data.model_dump(exclude_unset=True)))


@router.patch("/{paper_id}/items/{number}", response_model=PaperDetail)
async def update_item(paper_id: uuid.UUID, number: int, data: ItemUpdate, user: StaffUser, db: DbSession):
    return detail(await paper_service.update_item(db, user, paper_id, number, data))


@router.post("/{paper_id}/publish", response_model=PublishResult)
async def publish(paper_id: uuid.UUID, user: StaffUser, db: DbSession):
    """Ready questions go into the bank (school or library); skipped ones are listed by number."""
    paper, created, skipped = await paper_service.publish(db, user, paper_id)
    return PublishResult(paper=summary(paper), created=created, skipped=skipped)


@router.post("/{paper_id}/start", response_model=AttemptDetail, status_code=status.HTTP_201_CREATED)
async def start(paper_id: uuid.UUID, user: ActivePlanUser, db: DbSession):
    """Take the whole paper as a timed exam simulation, in the original order and time limit."""
    return quiz_service.to_detail(await paper_service.start_attempt(db, user, paper_id))


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(paper_id: uuid.UUID, user: CurrentUser, db: DbSession):
    await paper_service.delete_paper(db, user, paper_id)
