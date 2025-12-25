"""Grades and streams.

Ethiopian secondary school is grades 9-12. Grades 9-10 share a common curriculum; the
natural/social split starts in grade 11. So a student always has a grade and only has a stream
from grade 11 — and the two are checked as a pair everywhere they can be written.

Grade sets the *level* of content (nothing tagged above it); stream sets the *subjects*.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import PASSWORD, question_payload
from tests.test_papers import published_paper  # noqa: F401

REGISTER = "/api/v1/auth/register"
ME = "/api/v1/users/me"


def signup(client, code, email, **level):
    return client.post(
        REGISTER,
        json={"join_code": code, "full_name": "Grade Student", "email": email, "password": PASSWORD, **level},
    )


def student(client, school, email, **level):
    r = signup(client, school["code"], email, **level)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def me(client, headers):
    return client.get(ME, headers=headers).json()


def add_question(client, staff, subject="mathematics", grade=None, n=0):
    body = question_payload(subject=subject, topic=f"{subject} g{grade}", n=n)
    if grade is not None:
        body["grade"] = grade
    r = client.post("/api/v1/questions", json=body, headers=staff)
    assert r.status_code == 201, r.text
    return r.json()


def visible_grades(client, headers):
    rows = client.get("/api/v1/questions", params={"limit": 100}, headers=headers).json()
    return {q["grade"] for q in rows}


@pytest.fixture
def graded_bank(client, school):
    """One mathematics question per grade 9-12, plus one untagged. Mathematics is common to
    every stream, so only the grade ceiling decides what each student sees."""
    for n, grade in enumerate((9, 10, 11, 12, None)):
        add_question(client, school["teacher"], grade=grade, n=n)


# ---------- signup ----------


def test_grade_10_signs_up_without_a_stream(client, school):
    s = student(client, school, "g10@school.edu", grade=10)
    assert (me(client, s)["grade"], me(client, s)["stream"]) == (10, None)


def test_grade_12_signs_up_with_a_stream(client, school):
    s = student(client, school, "g12@school.edu", grade=12, stream="natural")
    assert (me(client, s)["grade"], me(client, s)["stream"]) == (12, "natural")


def test_a_stream_before_grade_11_is_refused(client, school):
    r = signup(client, school["code"], "early@school.edu", grade=10, stream="natural")
    assert r.status_code == 422
    assert "streams start in grade 11" in r.text


def test_grade_11_without_a_stream_is_refused(client, school):
    r = signup(client, school["code"], "nostream@school.edu", grade=11)
    assert r.status_code == 422
    assert "choose a stream" in r.text


@pytest.mark.parametrize("grade", [8, 13, 0])
def test_grades_outside_secondary_school_are_refused(client, school, grade):
    assert signup(client, school["code"], f"g{grade}@school.edu", grade=grade).status_code == 422


def test_accounts_without_a_grade_still_work(client, school):
    """Clients that predate grades send only a stream; nothing about them changes."""
    s = student(client, school, "legacy@school.edu", stream="social")
    assert (me(client, s)["grade"], me(client, s)["stream"]) == (None, "social")


def test_the_teacher_code_ignores_grade_and_stream(client, school):
    code = client.get("/api/v1/organizations/me", headers=school["admin"]).json()["teacher_code"]
    r = signup(client, code, "newteacher@school.edu", grade=12, stream="natural")
    assert r.status_code == 201, r.text
    t = me(client, {"Authorization": f"Bearer {r.json()['access_token']}"})
    assert (t["role"], t["grade"], t["stream"]) == ("teacher", None, None)


# ---------- moving between grades ----------


def test_moving_into_grade_11_needs_a_stream_in_the_same_request(client, school):
    s = student(client, school, "up@school.edu", grade=10)

    r = client.patch(ME, json={"grade": 11}, headers=s)
    assert r.status_code == 400
    assert "choose a stream" in r.json()["detail"]
    assert me(client, s)["grade"] == 10, "a refused change must not half-apply"

    r = client.patch(ME, json={"grade": 11, "stream": "social"}, headers=s)
    assert r.status_code == 200, r.text
    assert (r.json()["grade"], r.json()["stream"]) == (11, "social")


def test_moving_back_below_grade_11_clears_the_stream(client, school):
    s = student(client, school, "down@school.edu", grade=12, stream="natural")
    r = client.patch(ME, json={"grade": 10}, headers=s)
    assert r.status_code == 200, r.text
    assert (r.json()["grade"], r.json()["stream"]) == (10, None)


def test_grade_10_cannot_choose_a_stream(client, school):
    s = student(client, school, "g10s@school.edu", grade=10)
    r = client.patch(ME, json={"stream": "natural"}, headers=s)
    assert r.status_code == 400
    assert "streams start in grade 11" in r.json()["detail"]


def test_switching_stream_keeps_the_grade(client, school):
    s = student(client, school, "switch@school.edu", grade=12, stream="natural")
    r = client.patch(ME, json={"stream": "social"}, headers=s)
    assert (r.json()["grade"], r.json()["stream"]) == (12, "social")


def test_a_stream_cannot_be_removed_in_grade_12(client, school):
    s = student(client, school, "nullstream@school.edu", grade=12, stream="natural")
    assert client.patch(ME, json={"stream": None}, headers=s).status_code == 400


def test_an_empty_update_is_refused(client, school):
    s = student(client, school, "empty@school.edu", grade=10)
    assert client.patch(ME, json={}, headers=s).status_code == 422


def test_staff_have_no_grade(client, school):
    assert client.patch(ME, json={"grade": 12}, headers=school["teacher"]).status_code == 400


# ---------- the grade ceiling on content ----------


def test_grade_9_sees_only_grade_9_and_untagged_content(client, school, graded_bank):
    s = student(client, school, "nine@school.edu", grade=9)
    assert visible_grades(client, s) == {9, None}


def test_grade_12_sees_the_whole_syllabus(client, school, graded_bank):
    """The entrance exam draws on earlier grades, so nothing is hidden from grade 12."""
    s = student(client, school, "twelve@school.edu", grade=12, stream="natural")
    assert visible_grades(client, s) == {9, 10, 11, 12, None}


def test_the_ceiling_rises_with_the_grade(client, school, graded_bank):
    s = student(client, school, "riser@school.edu", grade=10)
    assert visible_grades(client, s) == {9, 10, None}
    client.patch(ME, json={"grade": 11, "stream": "natural"}, headers=s)
    assert visible_grades(client, s) == {9, 10, 11, None}


def test_practice_never_draws_above_grade(client, school, graded_bank):
    s = student(client, school, "practice@school.edu", grade=10)
    a = client.post("/api/v1/attempts", json={"size": 50}, headers=s).json()
    assert {i["question"]["grade"] for i in a["items"]} <= {9, 10, None}

    adaptive = client.post("/api/v1/attempts", json={"mode": "adaptive", "size": 50}, headers=s).json()
    assert {i["question"]["grade"] for i in adaptive["items"]} <= {9, 10, None}


def test_recommendations_respect_the_ceiling(client, school):
    """With only grade 12 material in the bank, a grade 9 student has nothing to be recommended."""
    add_question(client, school["teacher"], grade=12, n=1)
    s = student(client, school, "rec@school.edu", grade=9)
    assert client.get("/api/v1/learning/next-question", headers=s).status_code == 404

    add_question(client, school["teacher"], grade=9, n=2)
    r = client.get("/api/v1/learning/next-question", headers=s)
    assert r.status_code == 200 and r.json()["question"]["grade"] == 9


def test_staff_see_every_grade(client, school, graded_bank):
    assert visible_grades(client, school["teacher"]) == {9, 10, 11, 12, None}


def test_students_without_a_grade_have_no_ceiling(client, school, graded_bank):
    s = student(client, school, "nograde@school.edu", stream="natural")
    assert visible_grades(client, s) == {9, 10, 11, 12, None}


# ---------- subjects follow the stream, which follows the grade ----------


def test_grade_9_and_10_study_every_subject(client, school):
    for n, subject in enumerate(("mathematics", "physics", "history", "geography", "biology")):
        add_question(client, school["teacher"], subject=subject, n=n)
    s = student(client, school, "common@school.edu", grade=10)
    subjects = {q["subject"] for q in client.get("/api/v1/questions", params={"limit": 100}, headers=s).json()}
    assert subjects == {"mathematics", "physics", "history", "geography", "biology"}


def test_grade_11_studies_its_stream(client, school):
    for n, subject in enumerate(("mathematics", "physics", "history")):
        add_question(client, school["teacher"], subject=subject, n=n)
    s = student(client, school, "eleven@school.edu", grade=11, stream="social")
    subjects = {q["subject"] for q in client.get("/api/v1/questions", params={"limit": 100}, headers=s).json()}
    assert subjects == {"mathematics", "history"}


# ---------- papers and contests ----------


def test_an_entrance_paper_is_hidden_below_grade_12(client, school):
    paper = published_paper(client, school)
    assert paper["grade"] == 12, "the fixture paper is a grade 12 entrance exam"

    g10 = student(client, school, "paper10@school.edu", grade=10)
    g12 = student(client, school, "paper12@school.edu", grade=12, stream="natural")

    assert client.get("/api/v1/papers", headers=g10).json() == []
    assert client.get(f"/api/v1/papers/{paper['id']}", headers=g10).status_code == 404
    assert [p["id"] for p in client.get("/api/v1/papers", headers=g12).json()] == [paper["id"]]


def test_a_grade_12_contest_turns_away_younger_students(client, school):
    for n in range(3):
        add_question(client, school["teacher"], grade=12, n=n)
    now = datetime.now(timezone.utc)
    r = client.post(
        "/api/v1/contests",
        json={
            "title": "Entrance Sprint", "subject": "mathematics", "grade": 12,
            "duration_minutes": 10, "question_count": 3,
            "starts_at": (now - timedelta(minutes=1)).isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat(),
        },
        headers=school["teacher"],
    )
    assert r.status_code == 201, r.text
    contest = r.json()

    g10 = student(client, school, "c10@school.edu", grade=10)
    g12 = student(client, school, "c12@school.edu", grade=12, stream="natural")

    refused = client.post(f"/api/v1/contests/{contest['id']}/start", headers=g10)
    assert refused.status_code == 403
    assert "grade 12" in refused.json()["detail"]
    assert client.get(f"/api/v1/contests/{contest['id']}", headers=g10).json()["can_join"] is False

    assert client.post(f"/api/v1/contests/{contest['id']}/start", headers=g12).status_code == 200


# ---------- the database is the last line of defence ----------


@pytest.mark.parametrize(
    ("grade", "stream"),
    [(10, "natural"), (12, None), (8, None), (13, None)],
)
def test_the_database_refuses_an_impossible_grade_and_stream(client, school, database_url, grade, stream):
    """Even a code path that skips every validator cannot store these."""
    uid = me(client, school["student"])["id"]

    async def write():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET grade = :g, stream = :s WHERE id = :id"),
                    {"g": grade, "s": stream, "id": uid},
                )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(write())
