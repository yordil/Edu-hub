"""Ingestion worker: parses uploaded past papers off the request path.

    python -m app.worker

The queue is the exam_papers table. Each lane claims one waiting paper at a time with
SELECT ... FOR UPDATE SKIP LOCKED, parses it, and claims the next. Concurrency is the number of
lanes; more capacity is more lanes or more worker processes, on any machine that can reach the
database and the upload storage.

Nothing is held outside Postgres, so stopping or crashing a worker loses nothing: a paper caught
mid-parse is claimed again once it goes stale, and failed for good after repeated crashes.
"""

import asyncio
import contextlib
import logging
import signal
import uuid

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.services import paper_service

log = logging.getLogger("eduhub.worker")

# How long an idle lane waits before checking the table again. Parsing takes far longer than
# this, so polling adds no noticeable delay and needs no notification channel.
IDLE_SECONDS = 2.0
HOUSEKEEPING_SECONDS = 60.0


async def _sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _parse(paper_id: uuid.UUID) -> None:
    timeout = get_settings().worker_job_timeout_seconds
    async with get_sessionmaker()() as db:
        try:
            paper = await asyncio.wait_for(paper_service.process_paper(db, paper_id), timeout=timeout)
        except TimeoutError:
            await db.rollback()
            # A parse this slow will be just as slow next time, so fail it rather than retry.
            # The parsing thread can't be interrupted and runs on, but no further work is queued
            # behind it.
            await paper_service.fail_paper(
                db, paper_id, f"Processing took longer than {timeout // 60} minutes. Try a smaller or clearer file."
            )
            log.warning("paper timed out", extra={"paper_id": str(paper_id)})
            return
    log.info("parsed paper", extra={"paper_id": str(paper_id), "status": paper.status, "items": len(paper.items)})


async def _lane(number: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with get_sessionmaker()() as db:
                paper_id = await paper_service.claim_next(db)
            if paper_id is None:
                await _sleep_unless_stopped(stop, IDLE_SECONDS)
                continue
            # Not cancelled on shutdown: the current paper is allowed to finish first.
            await _parse(paper_id)
        except Exception:
            # A bug or a database blip must not kill the lane. The paper, if any, stays claimed
            # and is retried once stale.
            log.exception("lane %d failed; continuing", number)
            await _sleep_unless_stopped(stop, IDLE_SECONDS)


async def _housekeeping(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with get_sessionmaker()() as db:
                if given_up := await paper_service.give_up_exhausted(db):
                    log.warning("gave up on %d paper(s) after repeated crashes", given_up)
        except Exception:
            log.exception("housekeeping failed; continuing")
        await _sleep_unless_stopped(stop, HOUSEKEEPING_SECONDS)


async def main() -> None:
    configure_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not supported on Windows event loops; Ctrl+C still raises KeyboardInterrupt there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    lanes = get_settings().worker_max_jobs
    log.info("worker started with %d lane(s)", lanes)
    try:
        await asyncio.gather(_housekeeping(stop), *(_lane(i, stop) for i in range(lanes)))
    finally:
        await get_engine().dispose()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
