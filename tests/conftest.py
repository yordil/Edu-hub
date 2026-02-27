import asyncio
import os
from pathlib import Path

# Ryuk's port mapping fails on Docker Desktop for Windows; the `with` block below cleans up instead.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "audit_log, payment_transactions, subscriptions, "
    "exam_papers, question_embeddings, ai_guardrail_events, tutor_memories, "
    "contest_answers, contest_entries, contest_questions, contests, "
    "ai_usage, tutor_messages, tutor_conversations, announcements, topic_mastery, "
    "attempt_items, attempts, choices, questions, users, organizations"
)


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer("pgvector/pgvector:pg16", username="test", password="test", dbname="test") as pg:
        host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
        yield f"postgresql+asyncpg://test:test@{host}:{port}/test"


@pytest.fixture(scope="session")
def app(database_url):
    os.environ.update(
        DATABASE_URL=database_url, SECRET_KEY="t" * 48, ENVIRONMENT="test", CORS_ORIGINS="[]", LLM_PROVIDER="fake"
    )

    from app.core import db
    from app.core.config import get_settings

    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    from app.main import create_app

    return create_app()


async def _truncate(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    await engine.dispose()


@pytest.fixture
def client(app, database_url):
    asyncio.run(_truncate(database_url))
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def inline_parse(monkeypatch):
    """Parse uploaded papers inside the upload request.

    In production a worker claims the paper after the upload returns, so its questions don't
    exist yet. Most tests want the parsed result in one call, so the upload is followed by a parse
    on the same session; `process_paper` updates the instance the request is holding, so the
    response carries the items. `deferred_parse` restores the real, queue-only behaviour.
    """
    from app.services import paper_service

    original = paper_service.enqueue_paper

    async def enqueue_then_parse(db, *args, **kwargs):
        paper = await original(db, *args, **kwargs)
        return await paper_service.process_paper(db, paper.id)

    enqueue_then_parse.original = original
    monkeypatch.setattr(paper_service, "enqueue_paper", enqueue_then_parse)


@pytest.fixture
def deferred_parse(monkeypatch):
    """Leave uploads queued for a worker, as production does."""
    from app.services import paper_service

    monkeypatch.setattr(paper_service, "enqueue_paper", paper_service.enqueue_paper.original)


# ---------- helpers ----------

PASSWORD = "s3cret-pass!"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_org(client: TestClient, email: str = "admin@school.edu", name: str = "Springfield High") -> dict:
    r = client.post(
        "/api/v1/auth/organizations",
        json={"organization_name": name, "full_name": "Ada Admin", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return auth(r.json()["access_token"])


def join_code(client: TestClient, admin: dict) -> str:
    return client.get("/api/v1/organizations/me", headers=admin).json()["join_code"]


def register_student(client: TestClient, code: str, email: str = "stu@school.edu") -> dict:
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": code, "full_name": "Sam Student", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return auth(r.json()["access_token"])


def make_teacher(client: TestClient, admin: dict, code: str, email: str = "teach@school.edu") -> dict:
    headers = register_student(client, code, email)
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    r = client.patch(f"/api/v1/users/{user_id}/role", json={"role": "teacher"}, headers=admin)
    assert r.status_code == 200, r.text
    return headers


def question_payload(subject="Math", topic="Algebra", difficulty="easy", n=0) -> dict:
    return {
        "subject": subject,
        "topic": topic,
        "difficulty": difficulty,
        "prompt": f"What is {n} + 1?",
        "explanation": f"{n} plus one is {n + 1}.",
        "choices": [
            {"text": str(n + 1), "is_correct": True},
            {"text": str(n + 2)},
            {"text": str(n + 3)},
        ],
    }


def create_question(client: TestClient, staff: dict, **kw) -> dict:
    r = client.post("/api/v1/questions", json=question_payload(**kw), headers=staff)
    assert r.status_code == 201, r.text
    return r.json()


def subscribe(database_url: str, org_id: str, days: int = 30) -> None:
    """Give an org a paid plan, the way a settled payment would."""

    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO subscriptions (id, org_id, plan, status,"
                    " current_period_start, current_period_end, created_at, updated_at)"
                    " VALUES (gen_random_uuid(), :org, 'pro', 'active',"
                    " now(), now() + make_interval(days => :days), now(), now())"
                ),
                {"org": org_id, "days": days},
            )
        await engine.dispose()

    asyncio.run(run())


def org_id_of(client: TestClient, headers: dict) -> str:
    return client.get("/api/v1/users/me", headers=headers).json()["org_id"]


@pytest.fixture
def school(client, database_url):
    """An org with an admin, a teacher, a student and its join code.

    Carries a paid plan: the premium-gated routes are part of the normal surface most
    tests exercise, so unpaid is the special case that billing tests set up explicitly.
    """
    admin = create_org(client)
    code = join_code(client, admin)
    teacher = make_teacher(client, admin, code)
    student = register_student(client, code)
    subscribe(database_url, org_id_of(client, admin))
    return {"admin": admin, "teacher": teacher, "student": student, "code": code}


@pytest.fixture
def unpaid_school(client):
    """Same shape as `school`, with no subscription: premium routes must refuse."""
    admin = create_org(client, email="admin@free.edu", name="Free Academy")
    code = join_code(client, admin)
    teacher = make_teacher(client, admin, code, email="teach@free.edu")
    student = register_student(client, code, email="stu@free.edu")
    return {"admin": admin, "teacher": teacher, "student": student, "code": code}


@pytest.fixture
def platform_admin(client, database_url):
    """An EduHub operator, created the same way the CLI does it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import user_service

    async def create():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await user_service.create_platform_admin(db, "owner@eduhub.dev", PASSWORD, "Owner")
        await engine.dispose()

    asyncio.run(create())
    r = client.post("/api/v1/auth/login", data={"username": "owner@eduhub.dev", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return auth(r.json()["access_token"])
