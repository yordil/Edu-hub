import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine


def test_migrations_match_models(app, database_url):
    from app.core.db import Base

    async def diff():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.connect() as conn:
            result = await conn.run_sync(
                lambda c: compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata)
            )
        await engine.dispose()
        return result

    assert asyncio.run(diff()) == []


def test_health(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok"}


def test_request_id_is_echoed_or_generated(client):
    assert client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    generated = client.get("/health/live", headers={"X-Request-ID": "bad id!"}).headers["X-Request-ID"]
    assert generated != "bad id!" and len(generated) == 32
