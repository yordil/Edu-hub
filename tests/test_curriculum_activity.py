import asyncio
from datetime import date, timedelta

import pytest
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.activity_service import streaks
from tests.conftest import PASSWORD, create_question, register_student

ACTIVITY = "/api/v1/learning/activity"


def register(client, code, email, stream=None):
    body = {"join_code": code, "full_name": "Stream Student", "email": email, "password": PASSWORD}
    if stream:
        body["stream"] = stream
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def bank(client, school):
    """One question in each of the 8 curriculum subjects."""
    subjects = ["mathematics", "english", "sat", "physics", "chemistry", "biology", "history", "geography"]
    return {s: create_question(client, school["teacher"], subject=s, topic=f"{s} basics", n=i) for i, s in enumerate(subjects)}


# ---------- curriculum ----------


def test_curriculum_endpoint(client):
    c = client.get("/api/v1/learning/curriculum").json()
    assert c["subjects"] == ["mathematics", "english", "sat", "physics", "chemistry", "biology", "history", "geography"]
    assert c["streams"]["natural"] == ["mathematics", "english", "sat", "physics", "chemistry", "biology"]
    assert c["streams"]["social"] == ["mathematics", "english", "sat", "history", "geography"]


@pytest.mark.parametrize("subject", ["computer science", "algorithms", "art"])
def test_unknown_subjects_rejected(client, school, subject):
    from tests.conftest import question_payload

    r = client.post("/api/v1/questions", json=question_payload(subject=subject), headers=school["teacher"])
    assert r.status_code == 422


@pytest.mark.parametrize(("alias", "canonical"), [("Maths", "mathematics"), ("BIO", "biology"), ("Chem", "chemistry"), ("SAT", "sat")])
def test_subject_aliases(client, school, alias, canonical):
    assert create_question(client, school["teacher"], subject=alias)["subject"] == canonical


# ---------- streams ----------


def visible_subjects(client, headers):
    return {q["subject"] for q in client.get("/api/v1/questions", params={"limit": 100}, headers=headers).json()}


def test_natural_and_social_students_see_their_subjects(client, school, bank):
    natural = register(client, school["code"], "nat@school.edu", "natural")
    social = register(client, school["code"], "soc@school.edu", "social")

    assert visible_subjects(client, natural) == {"mathematics", "english", "sat", "physics", "chemistry", "biology"}
    assert visible_subjects(client, social) == {"mathematics", "english", "sat", "history", "geography"}
    assert visible_subjects(client, school["teacher"]) == set(bank)
    assert client.get("/api/v1/users/me", headers=natural).json()["stream"] == "natural"


def test_streams_apply_to_practice_and_recommendations(client, school, bank):
    social = register(client, school["code"], "soc@school.edu", "social")

    a = client.post("/api/v1/attempts", json={"size": 50}, headers=social).json()
    assert {i["question"]["subject"] for i in a["items"]} == {"mathematics", "english", "sat", "history", "geography"}

    adaptive = client.post("/api/v1/attempts", json={"mode": "adaptive", "size": 50}, headers=social).json()
    assert "physics" not in {i["question"]["subject"] for i in adaptive["items"]}

    assert client.post("/api/v1/attempts", json={"subject": "physics"}, headers=social).status_code == 404
    assert client.get("/api/v1/learning/next-question", params={"subject": "chemistry"}, headers=social).status_code == 404
    assert client.get("/api/v1/learning/next-question", params={"subject": "history"}, headers=social).status_code == 200


def test_switch_stream(client, school, bank):
    student = register(client, school["code"], "s@school.edu", "natural")
    r = client.patch("/api/v1/users/me", json={"stream": "social"}, headers=student)
    assert r.status_code == 200 and r.json()["stream"] == "social"
    assert "history" in visible_subjects(client, student)
    assert client.patch("/api/v1/users/me", json={"stream": "arts"}, headers=student).status_code == 422
    assert client.patch("/api/v1/users/me", json={"stream": "social"}, headers=school["teacher"]).status_code == 400


def test_teacher_code_ignores_stream(client, school):
    code = client.get("/api/v1/organizations/me", headers=school["admin"]).json()["teacher_code"]
    teacher = register(client, code, "t2@school.edu", "natural")
    me = client.get("/api/v1/users/me", headers=teacher).json()
    assert (me["role"], me["stream"]) == ("teacher", None)


def test_invalid_stream_at_signup(client, school):
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": school["code"], "full_name": "X", "email": "x@school.edu", "password": PASSWORD, "stream": "engineering"},
    )
    assert r.status_code == 422


# ---------- streaks (pure) ----------

TODAY = date(2026, 10, 7)


def days(*offsets):
    return {TODAY - timedelta(days=o) for o in offsets}


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        (set(), (0, 0)),
        (days(0), (1, 1)),
        (days(0, 1, 2), (3, 3)),
        (days(1, 2), (2, 2)),  # yesterday still keeps the streak alive
        (days(2, 3, 4), (0, 3)),  # a full missed day breaks it
        (days(0, 1, 5, 6, 7, 8), (2, 4)),
    ],
)
def test_streaks(active, expected):
    assert streaks(active, TODAY) == expected


# ---------- activity endpoint ----------


def submit_session(client, headers, bank, subject, correct=True):
    q = bank[subject]
    a = client.post("/api/v1/attempts", json={"subject": subject}, headers=headers).json()
    choice = next(c for c in q["choices"] if c["is_correct"] == correct)
    client.post(f"/api/v1/attempts/{a['id']}/submit", json={"answers": [{"question_id": q["id"], "choice_id": choice["id"]}]}, headers=headers)
    return a["id"]


def backdate(database_url, attempt_id, days_ago):
    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempts SET submitted_at = submitted_at - make_interval(days => :d) WHERE id = :id"),
                {"d": days_ago, "id": attempt_id},
            )
        await engine.dispose()

    asyncio.run(run())


def test_activity_empty(client, school):
    a = client.get(ACTIVITY, params={"days": 30}, headers=school["student"]).json()
    assert len(a["days"]) == 30
    assert (a["current_streak"], a["longest_streak"], a["active_days"], a["total_questions"]) == (0, 0, 0, 0)


def test_activity_counts_and_streaks(client, school, bank, database_url):
    s = school["student"]
    submit_session(client, s, bank, "mathematics", correct=True)
    submit_session(client, s, bank, "english", correct=False)
    for days_ago in (1, 2, 5):
        backdate(database_url, submit_session(client, s, bank, "sat"), days_ago)
    client.post("/api/v1/attempts", json={"subject": "physics"}, headers=s)  # unsubmitted: ignored

    a = client.get(ACTIVITY, params={"days": 7}, headers=s).json()
    by_offset = {i: d for i, d in enumerate(reversed(a["days"]))}
    assert (by_offset[0]["sessions"], by_offset[0]["questions"], by_offset[0]["correct"]) == (2, 2, 1)
    assert by_offset[1]["sessions"] == by_offset[2]["sessions"] == by_offset[5]["sessions"] == 1
    assert by_offset[3]["sessions"] == by_offset[4]["sessions"] == 0
    assert (a["current_streak"], a["longest_streak"], a["active_days"], a["total_questions"]) == (3, 3, 4, 5)

    classmate = register_student(client, school["code"], "classmate@school.edu")
    assert client.get(ACTIVITY, headers=classmate).json()["total_questions"] == 0


def test_activity_window_bounds(client, school):
    assert client.get(ACTIVITY, params={"days": 3}, headers=school["student"]).status_code == 422
    assert client.get(ACTIVITY, params={"days": 400}, headers=school["student"]).status_code == 422
    assert len(client.get(ACTIVITY, headers=school["student"]).json()["days"]) == 365
