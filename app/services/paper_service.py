"""Past papers: upload a PDF (+ answer key), review the parsed questions, publish to the bank, take as a timed exam."""

import asyncio
import math
import uuid
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.ai import guardrails
from app.core.config import get_settings
from app.core.curriculum import normalize_tag
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, TooManyRequests, ValidationFailed
from app.models.base import utcnow
from app.models.paper import ExamPaper
from app.models.quiz import Attempt, AttemptItem, AttemptMode, Difficulty, Question
from app.models.user import Role, User
from app.schemas.paper import ItemUpdate, PaperUpload
from app.schemas.quiz import ChoiceIn, QuestionCreate
from app.services import audit_service, learning_service, pdf_parser, quiz_service
from app.services import knowledge_model as km
from app.services.question_filters import grade_ceiling, stream_subjects
from app.services.storage import StorageError, get_storage, paper_key

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
LETTERS = pdf_parser.LETTERS


def _blocking_parse(exam: bytes, key_pdf: bytes | None) -> tuple[pdf_parser.Extraction, pdf_parser.ParsedPaper, str | None]:
    ex = pdf_parser.extract(exam)
    paper = pdf_parser.parse_paper(ex)
    key_text = pdf_parser.text_of(key_pdf) if key_pdf else None
    return ex, paper, key_text


async def _uploads_last_hour(db: AsyncSession, user: User) -> int:
    since = utcnow() - timedelta(hours=1)
    return await db.scalar(
        select(func.count())
        .select_from(ExamPaper)
        .where(ExamPaper.created_by_id == user.id, ExamPaper.created_at >= since)
    ) or 0


async def enqueue_paper(
    db: AsyncSession, user: User, meta: PaperUpload, filename: str, exam: bytes, key_pdf: bytes | None, key_text: str | None
) -> ExamPaper:
    """Accept an upload and hand the parsing to a worker.

    Only checks that cost nothing run here. Extraction and OCR are minutes of CPU on a large
    scan, and doing that inline would let a handful of simultaneous uploads exhaust the web
    process's thread pool and stall unrelated requests.
    """
    settings = get_settings()
    if meta.library and not user.is_platform_admin:
        raise ForbiddenError("Only EduHub admins can add papers to the shared library")

    limit = settings.paper_uploads_per_hour
    if await _uploads_last_hour(db, user) >= limit:
        raise TooManyRequests(f"You can upload {limit} papers per hour. Try again later.")

    mb = settings.paper_max_upload_bytes // (1024 * 1024)
    for name, blob in (("exam", exam), ("answer key", key_pdf)):
        if blob is not None and len(blob) > settings.paper_max_upload_bytes:
            raise ValidationFailed(f"The {name} file is larger than {mb} MB")
    try:
        page_count = await asyncio.to_thread(pdf_parser.probe, exam)
        if key_pdf is not None:
            await asyncio.to_thread(pdf_parser.probe, key_pdf)
    except pdf_parser.PdfError as e:
        raise ValidationFailed(str(e)) from None

    paper = ExamPaper(
        org_id=None if meta.library else user.org_id,
        created_by_id=user.id,
        status="queued",
        filename=filename[:255],
        # Placeholder until the worker reads the paper's own header.
        title=(meta.title or filename.rsplit(".", 1)[0])[:200],
        subject=meta.subject,
        grade=meta.grade,
        year=meta.year,
        time_limit_minutes=meta.time_limit_minutes,
        default_topic=meta.default_topic or "general",
        page_count=page_count,
        upload_meta=meta.model_dump(mode="json"),
        pasted_key=key_text,
        items=[],
        warnings=[],
        question_ids=[],
    )
    db.add(paper)
    await db.flush()

    store = get_storage()
    paper.storage_key = paper_key(paper.id, filename, "exam")
    await store.put(paper.storage_key, exam)
    if key_pdf is not None:
        paper.key_storage_key = paper_key(paper.id, "key.pdf", "key")
        await store.put(paper.key_storage_key, key_pdf)

    audit_service.record(
        db, user, "paper.uploaded", "exam_paper", paper.id,
        org_id=paper.org_id, filename=paper.filename, pages=page_count, library=meta.library,
    )
    # Committing the row is the whole hand-off: a worker claims it from the table.
    await db.commit()
    return paper


MAX_PARSE_ATTEMPTS = 3


async def claim_next(db: AsyncSession) -> uuid.UUID | None:
    """Take the oldest paper waiting to be parsed, or None if there is nothing to do.

    The table is the queue. SKIP LOCKED lets any number of workers claim concurrently without
    two of them taking the same row, and because nothing lives outside Postgres, a restart can't
    lose a job. A paper left in "processing" by a worker that died is claimable again once it
    goes stale.
    """
    cutoff = utcnow() - timedelta(minutes=get_settings().paper_stuck_after_minutes)
    paper = await db.scalar(
        select(ExamPaper)
        .where(
            or_(
                ExamPaper.status == "queued",
                (ExamPaper.status == "processing") & (ExamPaper.processing_started_at < cutoff),
            ),
            ExamPaper.attempts < MAX_PARSE_ATTEMPTS,
        )
        .order_by(ExamPaper.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if paper is None:
        await db.rollback()
        return None
    paper.status = "processing"
    paper.processing_started_at = utcnow()
    paper.attempts += 1
    await db.commit()
    return paper.id


async def give_up_exhausted(db: AsyncSession) -> int:
    """Fail papers that crashed a worker on every attempt, so they stop being retried and the
    teacher sees a reason instead of a paper stuck at "processing" forever."""
    cutoff = utcnow() - timedelta(minutes=get_settings().paper_stuck_after_minutes)
    result = await db.execute(
        update(ExamPaper)
        .where(
            ExamPaper.status == "processing",
            ExamPaper.processing_started_at < cutoff,
            ExamPaper.attempts >= MAX_PARSE_ATTEMPTS,
        )
        .values(
            status="failed",
            failure_reason=f"Processing stopped unexpectedly {MAX_PARSE_ATTEMPTS} times. Try uploading the file again.",
        )
    )
    await db.commit()
    return result.rowcount or 0


async def process_paper(db: AsyncSession, paper_id: uuid.UUID) -> ExamPaper:
    """Parse an upload. Safe to run more than once: it rebuilds `items` from the stored bytes, so
    a retry after a crash converges on the same result."""
    paper = await db.get(ExamPaper, paper_id)
    if paper is None:
        raise NotFoundError("Paper not found")
    if paper.status in ("review", "published"):
        return paper
    if not paper.storage_key:
        return await _fail(db, paper, "The uploaded file is missing.")

    store = get_storage()
    try:
        exam = await store.get(paper.storage_key)
        key_pdf = await store.get(paper.key_storage_key) if paper.key_storage_key else None
    except StorageError as e:
        return await _fail(db, paper, str(e))

    try:
        ex, parsed, key_from_pdf = await asyncio.to_thread(_blocking_parse, exam, key_pdf)
    except pdf_parser.PdfError as e:
        return await _fail(db, paper, str(e))
    if not parsed.items:
        return await _fail(db, paper, "No numbered questions were found in this PDF. Is it a multiple-choice paper?")

    meta = PaperUpload.model_validate(paper.upload_meta or {})
    key_text = paper.pasted_key
    warnings: list[str] = []
    if ex.ocr_used:
        warnings.append("Scanned paper: text was read with OCR, so check spelling and symbols carefully.")
    m = parsed.meta
    if m.expected_count and m.expected_count != len(parsed.items):
        warnings.append(f"The paper says {m.expected_count} questions but {len(parsed.items)} were found.")

    source = key_text or key_from_pdf or parsed.key_text
    key: dict[int, int] = {}
    if source:
        key, key_warnings = pdf_parser.parse_answer_key(source)
        warnings += key_warnings
        if not key:
            warnings.append("No answers could be read from the answer key.")
    elif parsed.answers:
        key = {n: a.answer for n, a in parsed.answers.items() if a.answer is not None}
        warnings.append(f"Answers and explanations were read from the paper's own answer section ({len(key)} answers).")
    if key:
        numbers = {i.number for i in parsed.items}
        if extra := sorted(set(key) - numbers):
            warnings.append(f"The answer key has answers for questions that weren't found: {', '.join(map(str, extra[:10]))}.")
    else:
        warnings.append("No answer key was provided. Set each answer before publishing.")

    items = []
    flagged: list[int] = []
    for it in parsed.items:
        issues = list(it.issues)
        answer = key.get(it.number)
        section = parsed.answers.get(it.number)
        if key and answer is None:
            issues.append("no answer in the key")
        elif answer is not None and answer >= len(it.choices):
            issues.append(f"the key says {LETTERS[answer]} but only {len(it.choices)} choices were found")
            answer = None
        if _moderation_issue(it.prompt, it.choices):
            issues.append(_MODERATION_ISSUE)
            flagged.append(it.number)
        items.append(
            {"number": it.number, "prompt": it.prompt, "choices": it.choices, "answer": answer, "topic": None,
             "include": True, "issues": issues, "page": it.page,
             "difficulty": it.difficulty or (section.difficulty if section else None),
             "explanation": section.explanation if section else None}
        )
    if flagged:
        warnings.append(
            f"{len(flagged)} question(s) were flagged for review before publishing: "
            f"{', '.join(map(str, flagged[:10]))}."
        )

    paper.status = "review"
    paper.title = (meta.title or m.title or paper.filename.rsplit(".", 1)[0])[:200]
    paper.subject = meta.subject or m.subject
    paper.grade = meta.grade or m.grade
    paper.year = meta.year or m.year
    paper.time_limit_minutes = meta.time_limit_minutes or m.time_limit_minutes
    paper.page_count = ex.page_count
    paper.scanned = ex.scanned
    paper.ocr_used = ex.ocr_used
    paper.has_key = bool(key)
    paper.items = items
    paper.warnings = warnings
    paper.failure_reason = None
    await db.commit()
    return paper


_MODERATION_ISSUE = "flagged by the content filter — read before publishing"


def _moderation_issue(prompt: str, choices: list[str]) -> bool:
    """Screen extracted text with the same filter the tutor uses.

    Parsing validates the *shape* of a paper, never what it says, so a file whose layout
    resembles an exam reaches the review queue whatever its content. This flags rather than
    rejects: the reviewing teacher decides, and a false positive must not block a real paper.
    """
    for text in (prompt, *choices):
        if guardrails.check_input(text).action == "block":
            return True
    return False


async def _fail(db: AsyncSession, paper: ExamPaper, reason: str) -> ExamPaper:
    paper.status = "failed"
    paper.failure_reason = reason
    await db.commit()
    return paper


async def fail_paper(db: AsyncSession, paper_id: uuid.UUID, reason: str) -> None:
    paper = await db.get(ExamPaper, paper_id)
    if paper is not None and paper.status not in ("review", "published"):
        await _fail(db, paper, reason)


def _can_manage(paper: ExamPaper, user: User) -> bool:
    if paper.org_id is None:
        return user.is_platform_admin
    return paper.org_id == user.org_id and user.role in (Role.teacher, Role.admin)


def _can_view(paper: ExamPaper, user: User) -> bool:
    if _can_manage(paper, user):
        return True
    if paper.status != "published" or paper.org_id not in (None, user.org_id):
        return False
    ceiling = grade_ceiling(user)
    if ceiling is not None and paper.grade is not None and paper.grade > ceiling:
        return False
    allowed = stream_subjects(user)
    return allowed is None or paper.subject in allowed


async def get_paper(db: AsyncSession, user: User, paper_id: uuid.UUID, manage: bool = False) -> ExamPaper:
    paper = await db.get(ExamPaper, paper_id)
    if paper is None or not (_can_manage(paper, user) if manage else _can_view(paper, user)):
        raise NotFoundError("Paper not found")
    return paper


async def list_papers(db: AsyncSession, user: User) -> list[ExamPaper]:
    stmt = select(ExamPaper).where(or_(ExamPaper.org_id.is_(None), ExamPaper.org_id == user.org_id))
    rows = await db.scalars(stmt.order_by(ExamPaper.created_at.desc(), ExamPaper.id))
    return [p for p in rows if _can_view(p, user)]


def item_problems(item: dict) -> list[str]:
    """What blocks this item from publishing (parse issues are advisory; these are hard rules)."""
    problems = []
    if item["answer"] is None:
        problems.append("no answer set")
    if not 2 <= len(item["choices"]) <= 6:
        problems.append("needs 2 to 6 choices")
    if len(item["prompt"].strip()) < 3:
        problems.append("question text is empty")
    if any(not c.strip() or len(c) > 500 for c in item["choices"]):
        problems.append("a choice is empty or too long")
    if len({normalize_tag(c) for c in item["choices"]}) != len(item["choices"]):
        problems.append("choices must be different")
    return problems


async def update_item(db: AsyncSession, user: User, paper_id: uuid.UUID, number: int, data: ItemUpdate) -> ExamPaper:
    paper = await get_paper(db, user, paper_id, manage=True)
    if paper.status != "review":
        raise ConflictError("This paper is already published")
    item = next((i for i in paper.items if i["number"] == number), None)
    if item is None:
        raise NotFoundError(f"Question {number} is not in this paper")
    changes = data.model_dump(exclude_unset=True)
    if "choices" in changes:
        old = item["choices"]
        item["choices"] = [c.strip() for c in changes.pop("choices")]
        item["answer"] = _follow_answer(old, item["answer"], item["choices"])
    if "answer" in changes and changes["answer"] is not None and changes["answer"] >= len(item["choices"]):
        raise ValidationFailed(f"Question {number} has only {len(item['choices'])} choices")
    if "topic" in changes and changes["topic"] is not None:
        changes["topic"] = normalize_tag(changes["topic"]) or None
    item.update(changes)
    # Edited by a person: parser warnings no longer apply.
    item["issues"] = []
    flag_modified(paper, "items")
    await db.commit()
    return paper


def _follow_answer(old: list[str], answer: int | None, new: list[str]) -> int | None:
    """Where the answer key points after the choices change.

    The key is stored as an index, so keeping the number would silently re-point it at whatever
    choice now sits there — reorder ["Oxygen", "Hydrogen"] and "Hydrogen" quietly becomes
    "Oxygen". It follows the *text* it named instead. If that choice was removed, or now appears
    twice, the key is cleared, and publishing stays blocked until a person sets it again.
    """
    if answer is None or answer >= len(old):
        return None
    target = normalize_tag(old[answer])
    matches = [i for i, c in enumerate(new) if normalize_tag(c) == target]
    return matches[0] if len(matches) == 1 else None


async def update_paper(db: AsyncSession, user: User, paper_id: uuid.UUID, changes: dict) -> ExamPaper:
    paper = await get_paper(db, user, paper_id, manage=True)
    for k, v in changes.items():
        setattr(paper, k, v)
    await db.commit()
    return paper


async def publish(db: AsyncSession, user: User, paper_id: uuid.UUID) -> tuple[ExamPaper, int, list[int]]:
    paper = await db.get(ExamPaper, paper_id, with_for_update=True)
    if paper is None or not _can_manage(paper, user):
        raise NotFoundError("Paper not found")
    if paper.status != "review":
        raise ConflictError("This paper is already published")
    if not paper.subject:
        raise ValidationFailed("Set the paper's subject before publishing")

    drafts: list[tuple[int, QuestionCreate]] = []
    skipped: list[int] = []
    for item in paper.items:
        if not item["include"]:
            continue
        if item_problems(item):
            skipped.append(item["number"])
            continue
        try:
            drafts.append(
                (
                    item["number"],
                    QuestionCreate(
                        subject=paper.subject,
                        topic=item["topic"] or paper.default_topic,
                        difficulty=Difficulty(item.get("difficulty") or "medium"),
                        grade=paper.grade,
                        source=f"{paper.title} · Q{item['number']}"[:200],
                        prompt=item["prompt"],
                        explanation=item.get("explanation"),
                        choices=[ChoiceIn(text=c, is_correct=i == item["answer"]) for i, c in enumerate(item["choices"])],
                    ),
                )
            )
        except ValidationError:
            skipped.append(item["number"])
    if not drafts:
        raise ValidationFailed("No question is ready to publish: every included question needs an answer and 2 to 6 distinct choices")

    questions = await quiz_service.create_questions(db, [d for _, d in drafts], user, paper.org_id)
    paper.question_ids = [str(q.id) for q in questions]
    paper.status = "published"
    paper.published_at = utcnow()
    # This is the moment content becomes visible to students, so it is the moment worth attributing.
    flagged = [i["number"] for i in paper.items if _MODERATION_ISSUE in i.get("issues", [])]
    audit_service.record(
        db, user, "paper.published", "exam_paper", paper.id,
        org_id=paper.org_id, created=len(questions), skipped=skipped,
        title=paper.title, flagged_published=[n for n in flagged if n not in skipped],
    )
    await db.commit()
    return paper, len(questions), skipped


async def start_attempt(db: AsyncSession, user: User, paper_id: uuid.UUID) -> Attempt:
    """The full paper, in the original order, under the original time limit."""
    paper = await get_paper(db, user, paper_id)
    if paper.status != "published":
        raise ConflictError("Publish the paper before taking it")
    ids = [uuid.UUID(i) for i in paper.question_ids]
    by_id = {q.id: q for q in await db.scalars(select(Question).where(Question.id.in_(ids)))}
    questions = [by_id[i] for i in ids if i in by_id]
    if not questions:
        raise NotFoundError("This paper's questions were removed")
    thetas = await learning_service.load_thetas(db, user.id)
    minutes = paper.time_limit_minutes or math.ceil(len(questions) * quiz_service.EXAM_MINUTES_PER_QUESTION)
    attempt = Attempt(
        org_id=user.org_id,
        user_id=user.id,
        mode=AttemptMode.exam,
        subject=paper.subject,
        deadline=utcnow() + timedelta(minutes=minutes),
        items=[
            AttemptItem(question=q, position=i, predicted_p=km.predict(thetas.get((q.subject, q.topic), 0.0), q.rating))
            for i, q in enumerate(questions)
        ],
    )
    db.add(attempt)
    await db.commit()
    return attempt


async def delete_paper(db: AsyncSession, user: User, paper_id: uuid.UUID) -> None:
    """Unpublished papers only — queued, in review, or failed. Published questions live on in the
    bank and in students' history. The stored upload goes with the row."""
    paper = await get_paper(db, user, paper_id, manage=True)
    if paper.status == "published":
        raise ConflictError("Published papers can't be deleted")
    if paper.status == "processing":
        raise ConflictError("This paper is being processed. Try again in a moment.")
    keys = [k for k in (paper.storage_key, paper.key_storage_key) if k]
    audit_service.record(
        db, user, "paper.deleted", "exam_paper", paper.id,
        org_id=paper.org_id, filename=paper.filename, status=paper.status,
    )
    await db.delete(paper)
    await db.commit()
    # After the commit: if it failed, the row and its files both survive. A file left behind by a
    # failed delete is harmless, and the bucket's lifecycle rule expires it.
    store = get_storage()
    for key in keys:
        await store.delete(key)
