"""Embed every question missing a vector for the current model: `python -m app.scripts.embed_questions`. Idempotent."""

import asyncio
import time

from app.ai.embeddings import get_embedder
from app.core.db import get_engine, get_sessionmaker
from app.services import semantic_service


async def main() -> None:
    started = time.perf_counter()
    async with get_sessionmaker()() as db:
        count = await semantic_service.backfill(db)
    await get_engine().dispose()
    print(f"embedded {count} question(s) with {get_embedder().model} in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
