import pytest

from tests.conftest import create_org, create_question

ATTEMPTS = "/api/v1/attempts"
MASTERY = "/api/v1/learning/mastery"
NEXT = "/api/v1/learning/next-question"


def answer_all(client, student, attempt, bank: dict, correct: bool) -> dict:
    """Submit every item in `attempt` right or wrong, using the teacher-side answer key in `bank`."""
    answers = []
    for item in attempt["items"]:
        q = bank[item["question"]["id"]]
        choice = next(c for c in q["choices"] if c["is_correct"] == correct)
        answers.append({"question_id": q["id"], "choice_id": choice["id"]})
    r = client.post(f"{ATTEMPTS}/{attempt['id']}/submit", json={"answers": answers}, headers=student)
    assert r.status_code == 200, r.text
    return r.json()


def practice(client, student, bank, correct: bool, **filters) -> dict:
    r = client.post(ATTEMPTS, json=filters, headers=student)
    assert r.status_code == 201, r.text
    return answer_all(client, student, r.json(), bank, correct)


def test_mastery_starts_empty(client, school):
    assert client.get(MASTERY, headers=school["student"]).json() == []


def test_submission_updates_mastery_and_question_rating(client, school):
    q = create_question(client, school["teacher"], difficulty="medium")
    assert q["rating"] == 0.0 and q["rating_count"] == 0

    started = client.post(ATTEMPTS, json={"mode": "adaptive"}, headers=school["student"]).json()
    assert started["mode"] == "adaptive"
    assert started["items"][0]["predicted_success"] == 0.5

    answer_all(client, school["student"], started, {q["id"]: q}, correct=True)

    mastery = client.get(MASTERY, headers=school["student"]).json()
    assert len(mastery) == 1
    m = mastery[0]
    assert (m["subject"], m["topic"], m["answered"], m["correct"], m["level"]) == ("mathematics", "algebra", 1, 1, "new")
    assert m["mastery"] == pytest.approx(0.5987, abs=1e-4)

    after = client.get(f"/api/v1/questions/{q['id']}", headers=school["teacher"]).json()
    assert after["rating"] == pytest.approx(-0.2)
    assert after["rating_count"] == 1


def test_initial_ratings_follow_difficulty(client, school):
    ratings = {d: create_question(client, school["teacher"], difficulty=d, n=i)["rating"] for i, d in enumerate(["easy", "medium", "hard"])}
    assert ratings == {"easy": -1.0, "medium": 0.0, "hard": 1.0}


def test_level_and_order_after_more_evidence(client, school):
    teacher, student = school["teacher"], school["student"]
    alg = {q["id"]: q for q in (create_question(client, teacher, topic="algebra", difficulty="medium", n=n) for n in range(3))}
    geo = {q["id"]: q for q in (create_question(client, teacher, topic="geometry", difficulty="medium", n=n + 10) for n in range(3))}
    practice(client, student, alg, correct=True, topic="algebra")
    practice(client, student, geo, correct=False, topic="geometry")

    mastery = client.get(MASTERY, headers=student).json()
    assert [m["topic"] for m in mastery] == ["geometry", "algebra"]
    assert mastery[0]["level"] == "struggling" and mastery[0]["correct"] == 0
    assert mastery[1]["level"] in ("proficient", "mastered") and mastery[1]["answered"] == 3


def test_skipped_items_count_as_wrong_for_mastery(client, school):
    create_question(client, school["teacher"])
    a = client.post(ATTEMPTS, json={}, headers=school["student"]).json()
    client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": []}, headers=school["student"])
    m = client.get(MASTERY, headers=school["student"]).json()[0]
    assert m["answered"] == 1 and m["correct"] == 0 and m["mastery"] < 0.5


def test_unsubmitted_attempts_do_not_change_mastery(client, school):
    create_question(client, school["teacher"])
    client.post(ATTEMPTS, json={"mode": "adaptive"}, headers=school["student"])
    assert client.get(MASTERY, headers=school["student"]).json() == []


def test_next_question_targets_seventy_percent_for_new_student(client, school):
    teacher = school["teacher"]
    easy = create_question(client, teacher, difficulty="easy", n=1)
    create_question(client, teacher, difficulty="medium", n=2)
    create_question(client, teacher, difficulty="hard", n=3)

    nxt = client.get(NEXT, headers=school["student"]).json()
    assert nxt["question"]["id"] == easy["id"]
    assert nxt["predicted_success"] == pytest.approx(0.7311, abs=1e-4)
    assert nxt["mastery"] == 0.5
    assert nxt["reason"] == "Predicted 73% chance of success in algebra (topic mastery 50%)"
    assert all(set(c) == {"id", "text"} for c in nxt["question"]["choices"])


def test_next_question_steers_to_weak_topic_and_fresh_questions(client, school):
    teacher, student = school["teacher"], school["student"]
    a1 = create_question(client, teacher, topic="algebra", difficulty="medium", n=1)
    g1 = create_question(client, teacher, topic="geometry", difficulty="medium", n=2)
    practice(client, student, {a1["id"]: a1}, correct=True, topic="algebra")
    practice(client, student, {g1["id"]: g1}, correct=False, topic="geometry")

    create_question(client, teacher, topic="algebra", difficulty="medium", n=3)
    create_question(client, teacher, topic="algebra", difficulty="easy", n=4)
    g2 = create_question(client, teacher, topic="geometry", difficulty="easy", n=5)

    nxt = client.get(NEXT, headers=student).json()
    assert nxt["question"]["id"] == g2["id"]
    assert nxt["mastery"] < 0.5


def test_next_question_filters_and_404(client, school):
    assert client.get(NEXT, headers=school["student"]).status_code == 404
    create_question(client, school["teacher"], subject="math")
    phys = create_question(client, school["teacher"], subject="physics", topic="motion")
    assert client.get(NEXT, params={"subject": "Physics"}, headers=school["student"]).json()["question"]["id"] == phys["id"]
    assert client.get(NEXT, params={"topic": "optics"}, headers=school["student"]).status_code == 404


def test_adaptive_attempt_avoids_questions_already_mastered(client, school):
    teacher, student = school["teacher"], school["student"]
    first = create_question(client, teacher, n=1)
    practice(client, student, {first["id"]: first}, correct=True)
    fresh = create_question(client, teacher, n=2)

    a = client.post(ATTEMPTS, json={"mode": "adaptive", "size": 1}, headers=student).json()
    assert [i["question"]["id"] for i in a["items"]] == [fresh["id"]]

    both = client.post(ATTEMPTS, json={"mode": "adaptive", "size": 5}, headers=student).json()
    assert [i["question"]["id"] for i in both["items"]] == [fresh["id"], first["id"]]


def test_mastery_is_per_student(client, school):
    from tests.conftest import register_student

    q = create_question(client, school["teacher"])
    practice(client, school["student"], {q["id"]: q}, correct=True)
    classmate = register_student(client, school["code"], "classmate@school.edu")
    assert client.get(MASTERY, headers=classmate).json() == []
    outsider = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(NEXT, headers=outsider).status_code == 404


def test_invalid_mode_rejected(client, school):
    assert client.post(ATTEMPTS, json={"mode": "chaos"}, headers=school["student"]).status_code == 422
