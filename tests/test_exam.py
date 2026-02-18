"""Exam simulation: up to 100 questions, a server-side deadline, autosaved answers and flags, and auto-close."""

import asyncio
from datetime import datetime

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import create_question

ATTEMPTS = "/api/v1/attempts"


def bank(client, school, n=4):
    return {q["id"]: q for q in (create_question(client, school["teacher"], n=i) for i in range(n))}


def start_exam(client, school, **kw):
    r = client.post(ATTEMPTS, json={"mode": "exam", "size": 100, **kw}, headers=school["student"])
    assert r.status_code == 201, r.text
    return r.json()


def choice(q, correct=True):
    return next(c["id"] for c in q["choices"] if c["is_correct"] == correct)


def expire(database_url, attempt_id):
    async def run():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE attempts SET deadline = now() - interval '5 minutes' WHERE id = :id"), {"id": attempt_id})
        await engine.dispose()

    asyncio.run(run())


def minutes(a):
    return round((datetime.fromisoformat(a["deadline"]) - datetime.fromisoformat(a["started_at"])).total_seconds() / 60)


def test_exam_serves_up_to_100_questions_with_a_default_deadline(client, school):
    bank(client, school, n=4)
    a = start_exam(client, school)
    assert a["mode"] == "exam" and a["total"] == 4
    assert minutes(a) == 6  # 1.5 minutes per question served
    assert all(i["flagged"] is False and i["selected_choice_id"] is None for i in a["items"])
    assert all(i["correct_choice_id"] is None for i in a["items"])


def test_exam_custom_time_limit(client, school):
    bank(client, school, n=2)
    assert minutes(start_exam(client, school, time_limit_minutes=45)) == 45
    assert client.post(ATTEMPTS, json={"mode": "exam", "time_limit_minutes": 0}, headers=school["student"]).status_code == 422
    assert client.post(ATTEMPTS, json={"mode": "exam", "time_limit_minutes": 241}, headers=school["student"]).status_code == 422


def test_practice_limits_unchanged(client, school):
    bank(client, school, n=2)
    s = school["student"]
    assert client.post(ATTEMPTS, json={"mode": "random", "size": 51}, headers=s).status_code == 422
    assert client.post(ATTEMPTS, json={"mode": "random", "size": 50}, headers=s).status_code == 201
    assert client.post(ATTEMPTS, json={"mode": "adaptive", "time_limit_minutes": 10}, headers=s).status_code == 422
    assert client.post(ATTEMPTS, json={"mode": "exam", "size": 101}, headers=s).status_code == 422
    assert client.post(ATTEMPTS, json={"size": 2}, headers=s).json()["deadline"] is None


def test_autosave_answers_and_flags_survive_reload(client, school):
    qs = bank(client, school, n=3)
    a = start_exam(client, school)
    first, second = (i["question"]["id"] for i in a["items"][:2])
    progress = {"items": [{"question_id": first, "choice_id": choice(qs[first]), "flagged": True}, {"question_id": second, "flagged": True}]}
    r = client.put(f"{ATTEMPTS}/{a['id']}/progress", json=progress, headers=school["student"])
    assert r.status_code == 200, r.text

    items = {i["question"]["id"]: i for i in client.get(f"{ATTEMPTS}/{a['id']}", headers=school["student"]).json()["items"]}
    assert items[first]["selected_choice_id"] == choice(qs[first]) and items[first]["flagged"] is True
    assert items[second]["selected_choice_id"] is None and items[second]["flagged"] is True
    assert items[first]["correct_choice_id"] is None  # still nothing revealed

    # Clearing an answer and unflagging.
    client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": [{"question_id": first, "choice_id": None, "flagged": False}]}, headers=school["student"])
    item = next(i for i in client.get(f"{ATTEMPTS}/{a['id']}", headers=school["student"]).json()["items"] if i["question"]["id"] == first)
    assert item["selected_choice_id"] is None and item["flagged"] is False


def test_autosave_validation_and_ownership(client, school):
    qs = bank(client, school, n=2)
    a = start_exam(client, school)
    qid = a["items"][0]["question"]["id"]
    other_q = next(i for i in qs if i != qid)
    url = f"{ATTEMPTS}/{a['id']}/progress"
    assert client.put(url, json={"items": [{"question_id": qid, "choice_id": choice(qs[other_q])}]}, headers=school["student"]).status_code == 400
    assert client.put(url, json={"items": [{"question_id": qid}, {"question_id": qid}]}, headers=school["student"]).status_code == 422
    assert client.put(url, json={"items": []}, headers=school["teacher"]).status_code == 404


def test_submit_merges_saved_and_submitted_answers(client, school):
    qs = bank(client, school, n=3)
    a = start_exam(client, school)
    ids = [i["question"]["id"] for i in a["items"]]
    client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": [{"question_id": ids[0], "choice_id": choice(qs[ids[0]])}]}, headers=school["student"])
    r = client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": [{"question_id": ids[1], "choice_id": choice(qs[ids[1]])}]}, headers=school["student"])
    assert r.status_code == 200
    assert r.json()["score"] == 2 and r.json()["total"] == 3
    assert client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": []}, headers=school["student"]).status_code == 409


def test_expired_exam_closes_with_saved_answers_only(client, school, database_url):
    qs = bank(client, school, n=3)
    a = start_exam(client, school)
    ids = [i["question"]["id"] for i in a["items"]]
    client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": [{"question_id": ids[0], "choice_id": choice(qs[ids[0]])}]}, headers=school["student"])
    expire(database_url, a["id"])

    assert client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": []}, headers=school["student"]).status_code == 409
    late = [{"question_id": q, "choice_id": choice(qs[q])} for q in ids]
    r = client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": late}, headers=school["student"])
    assert r.status_code == 200
    assert r.json()["score"] == 1  # late answers ignored; only the autosaved one counts


def test_expired_exam_auto_closes_on_open(client, school, database_url):
    qs = bank(client, school, n=2)
    a = start_exam(client, school)
    qid = a["items"][0]["question"]["id"]
    client.put(f"{ATTEMPTS}/{a['id']}/progress", json={"items": [{"question_id": qid, "choice_id": choice(qs[qid])}]}, headers=school["student"])
    expire(database_url, a["id"])

    body = client.get(f"{ATTEMPTS}/{a['id']}", headers=school["student"]).json()
    assert body["submitted_at"] is not None and body["score"] == 1
    assert all(i["correct_choice_id"] for i in body["items"])
    assert client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": []}, headers=school["student"]).status_code == 409
