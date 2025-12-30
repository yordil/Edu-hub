"""Semantic search with pgvector (hashing embedder in tests: deterministic, offline)."""

from datetime import datetime, timedelta, timezone

from app.ai.llm import text, tool_call
from tests.conftest import create_org, question_payload
from tests.test_tutor import fake_llm, tool_outputs  # noqa: F401  (fixture)

SEARCH = "/api/v1/questions/search"


def q(client, headers, subject, topic, prompt, explanation="", correct="Yes", n=0):
    body = {**question_payload(subject=subject, topic=topic, n=n), "prompt": prompt, "explanation": explanation}
    body["choices"] = [{"text": correct, "is_correct": True}, {"text": f"wrong {n}"}, {"text": f"other {n}"}]
    r = client.post("/api/v1/questions", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def register(client, code, email, stream):
    r = client.post("/api/v1/auth/register", json={"join_code": code, "full_name": "Search Kid", "email": email, "password": "s3cret-pass!", "stream": stream})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def bank(client, school):
    t = school["teacher"]
    return {
        "chem": q(client, t, "chemistry", "stoichiometry", "How do you balance a chemical equation using coefficients?", "Adjust coefficients so atoms balance.", "Change coefficients", 1),
        "moles": q(client, t, "chemistry", "stoichiometry", "How many moles are in 18 g of water?", "Divide mass by molar mass.", "1 mol", 2),
        "adwa": q(client, t, "history", "african history", "Who won the Battle of Adwa in 1896?", "Ethiopia defeated Italy.", "Ethiopia", 3),
        "newton": q(client, t, "physics", "mechanics", "What does Newton's second law state?", "Force equals mass times acceleration.", "F = ma", 4),
    }


def test_search_ranks_related_questions_first(client, school):
    b = bank(client, school)
    hits = client.get(SEARCH, params={"q": "balancing chemical equations"}, headers=school["teacher"]).json()
    assert hits[0]["question"]["id"] == b["chem"]["id"]
    assert hits[0]["similarity"] > hits[-1]["similarity"]
    assert all(set(c) == {"id", "text"} for h in hits for c in h["question"]["choices"])


def test_search_respects_streams_and_schools(client, school):
    bank(client, school)
    social = register(client, school["code"], "soc@school.edu", "social")
    subjects = {h["question"]["subject"] for h in client.get(SEARCH, params={"q": "Newton force chemical equation Adwa"}, headers=social).json()}
    assert subjects == {"history"}

    other = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(SEARCH, params={"q": "chemical equation"}, headers=other).json() == []


def test_similar_questions_exclude_self(client, school):
    b = bank(client, school)
    hits = client.get(f"/api/v1/questions/{b['chem']['id']}/similar", headers=school["teacher"]).json()
    ids = [h["question"]["id"] for h in hits]
    assert b["chem"]["id"] not in ids
    assert ids[0] == b["moles"]["id"]


def test_duplicate_check_before_saving(client, school):
    b = bank(client, school)
    hits = client.post(
        "/api/v1/questions/duplicates", json={"text": "How do you balance a chemical equation using coefficients?"}, headers=school["teacher"]
    ).json()
    assert hits and hits[0]["question"]["id"] == b["chem"]["id"] and hits[0]["similarity"] >= 0.8
    assert client.post("/api/v1/questions/duplicates", json={"text": "Completely unrelated volcano poetry"}, headers=school["teacher"]).json() == []
    assert client.post("/api/v1/questions/duplicates", json={"text": "balance equations"}, headers=school["student"]).status_code == 403


def test_live_contest_questions_hidden_from_student_search(client, school):
    b = bank(client, school)
    extra = q(client, school["teacher"], "chemistry", "stoichiometry", "What is a limiting reactant in a chemical equation?", "It runs out first.", "The one used up first", 5)
    now = datetime.now(timezone.utc)
    r = client.post(
        "/api/v1/contests",
        json={"title": "Chem cup", "subject": "chemistry", "question_count": 3, "duration_minutes": 10,
              "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat()},
        headers=school["teacher"],
    )
    assert r.status_code == 201, r.text
    chem_ids = {b["chem"]["id"], b["moles"]["id"], extra["id"]}

    def ids(headers):
        return {h["question"]["id"] for h in client.get(SEARCH, params={"q": "chemical equation moles reactant", "limit": 50}, headers=headers).json()}

    assert chem_ids <= ids(school["teacher"])
    assert not chem_ids & ids(school["student"])


def test_library_import_reports_duplicates(client, platform_admin):
    payload = [{**question_payload(subject="biology", topic="cells", n=1), "prompt": "Which organelle is the powerhouse of the cell?"}]
    first = client.post("/api/v1/platform/questions/import", json={"questions": payload}, headers=platform_admin).json()
    second = client.post("/api/v1/platform/questions/import", json={"questions": payload}, headers=platform_admin).json()
    assert first == {"created": 1, "possible_duplicates": 0}
    assert second == {"created": 1, "possible_duplicates": 1}


def test_rag_tool_only_reveals_answers_the_student_has_seen(client, school, fake_llm):
    b = bank(client, school)
    seen = b["chem"]
    a = client.post("/api/v1/attempts", json={"subject": "chemistry", "topic": "stoichiometry", "size": 50}, headers=school["student"]).json()
    answers = []
    for item in a["items"]:
        if item["question"]["id"] == seen["id"]:
            answers.append({"question_id": seen["id"], "choice_id": seen["choices"][0]["id"]})
    client.post(f"/api/v1/attempts/{a['id']}/submit", json={"answers": answers}, headers=school["student"])

    fake_llm.script = [tool_call("find_study_material", {"query": "balancing chemical equations"}), text("Here's how.")]
    client.post("/api/v1/tutor/chat", json={"message": "help with balancing"}, headers=school["student"])
    out = tool_outputs(fake_llm.requests[-1])[0]

    history = out["from_your_history"]
    assert history and history[0]["question"] == seen["prompt"] and history[0]["correct_answer"] == "Change coefficients"
    for item in out["new_practice_questions"]:
        assert set(item) == {"topic", "question", "relevance"}


def test_backfill_embeds_missing_questions(client, school, database_url):
    import asyncio

    from sqlalchemy import pool, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services import semantic_service

    bank(client, school)

    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM question_embeddings"))
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            n = await semantic_service.backfill(db)
        await engine.dispose()
        return n

    assert asyncio.run(run()) == 4
    assert client.get(SEARCH, params={"q": "Adwa"}, headers=school["teacher"]).json()[0]["question"]["subject"] == "history"


def test_search_validation(client, school):
    assert client.get(SEARCH, params={"q": "a"}, headers=school["student"]).status_code == 422
    assert client.get(SEARCH, params={"q": "atoms"}).status_code == 401
