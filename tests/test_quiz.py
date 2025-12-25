from tests.conftest import create_org, create_question, question_payload

QUESTIONS = "/api/v1/questions"
ATTEMPTS = "/api/v1/attempts"


def correct_choice_id(client, staff, question_id):
    q = client.get(f"{QUESTIONS}/{question_id}", headers=staff).json()
    return next(c["id"] for c in q["choices"] if c["is_correct"])


# ---------- question bank ----------


def test_teacher_creates_question_with_normalized_tags(client, school):
    q = create_question(client, school["teacher"], subject="  Math ", topic="Linear   EQUATIONS")
    assert q["subject"] == "mathematics"
    assert q["topic"] == "linear equations"
    assert [c["text"] for c in q["choices"]] == ["1", "2", "3"]
    assert sum(c["is_correct"] for c in q["choices"]) == 1


def test_students_cannot_author_questions(client, school):
    r = client.post(QUESTIONS, json=question_payload(), headers=school["student"])
    assert r.status_code == 403


def test_question_validation(client, school):
    bad = question_payload()
    bad["choices"] = [{"text": "a", "is_correct": True}, {"text": "b", "is_correct": True}]
    assert client.post(QUESTIONS, json=bad, headers=school["teacher"]).status_code == 422

    bad["choices"] = [{"text": "a"}, {"text": "b"}]
    assert client.post(QUESTIONS, json=bad, headers=school["teacher"]).status_code == 422

    bad["choices"] = [{"text": "Same", "is_correct": True}, {"text": " same "}]
    assert client.post(QUESTIONS, json=bad, headers=school["teacher"]).status_code == 422

    bad["choices"] = [{"text": "only", "is_correct": True}]
    assert client.post(QUESTIONS, json=bad, headers=school["teacher"]).status_code == 422


def test_students_never_see_answer_key(client, school):
    q = create_question(client, school["teacher"])
    listed = client.get(QUESTIONS, headers=school["student"]).json()
    single = client.get(f"{QUESTIONS}/{q['id']}", headers=school["student"]).json()
    for view in (listed[0], single):
        assert "explanation" not in view
        assert all(set(c) == {"id", "text"} for c in view["choices"])

    staff_view = client.get(f"{QUESTIONS}/{q['id']}", headers=school["teacher"]).json()
    assert staff_view["explanation"] == "0 plus one is 1."


def test_question_filters_and_pagination(client, school):
    for n in range(3):
        create_question(client, school["teacher"], subject="math", topic="algebra", n=n)
    create_question(client, school["teacher"], subject="physics", topic="motion", difficulty="hard")

    def ids(**params):
        return client.get(QUESTIONS, params=params, headers=school["student"]).json()

    assert len(ids()) == 4
    assert len(ids(subject="MATH")) == 3
    assert len(ids(difficulty="hard")) == 1
    assert len(ids(limit=2)) == 2
    assert len(ids(limit=2, offset=2)) == 2
    assert client.get(QUESTIONS, params={"limit": 0}, headers=school["student"]).status_code == 422


def test_questions_are_tenant_isolated(client, school):
    q = create_question(client, school["teacher"])
    outsider = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(QUESTIONS, headers=outsider).json() == []
    assert client.get(f"{QUESTIONS}/{q['id']}", headers=outsider).status_code == 404


# ---------- attempts ----------


def test_full_attempt_flow(client, school):
    teacher, student = school["teacher"], school["student"]
    for n in range(3):
        create_question(client, teacher, n=n)

    started = client.post(ATTEMPTS, json={"subject": "Math", "size": 10}, headers=student)
    assert started.status_code == 201
    attempt = started.json()
    assert attempt["total"] == 3
    assert attempt["submitted_at"] is None and attempt["score"] is None
    for item in attempt["items"]:
        assert item["correct_choice_id"] is None and item["is_correct"] is None and item["explanation"] is None
        assert all(set(c) == {"id", "text"} for c in item["question"]["choices"])

    items = attempt["items"]
    right = correct_choice_id(client, teacher, items[0]["question"]["id"])
    wrong = next(c["id"] for c in items[1]["question"]["choices"] if c["id"] != correct_choice_id(client, teacher, items[1]["question"]["id"]))
    answers = [
        {"question_id": items[0]["question"]["id"], "choice_id": right},
        {"question_id": items[1]["question"]["id"], "choice_id": wrong},
        # third question left unanswered -> counts as wrong
    ]
    r = client.post(f"{ATTEMPTS}/{attempt['id']}/submit", json={"answers": answers}, headers=student)
    assert r.status_code == 200
    result = r.json()
    assert result["score"] == 1 and result["total"] == 3
    assert result["submitted_at"] is not None
    assert [i["is_correct"] for i in result["items"]] == [True, False, False]
    assert result["items"][0]["correct_choice_id"] == right
    assert result["items"][2]["selected_choice_id"] is None
    assert result["items"][0]["explanation"].endswith("plus one is " + result["items"][0]["question"]["choices"][0]["text"] + ".")

    # Re-submitting is rejected.
    again = client.post(f"{ATTEMPTS}/{attempt['id']}/submit", json={"answers": answers}, headers=student)
    assert again.status_code == 409

    # History.
    history = client.get(ATTEMPTS, headers=student).json()
    assert len(history) == 1 and history[0]["score"] == 1
    assert client.get(f"{ATTEMPTS}/{attempt['id']}", headers=student).json()["items"][0]["is_correct"] is True


def test_attempt_respects_size_and_filters(client, school):
    for n in range(5):
        create_question(client, school["teacher"], subject="math", n=n)
    create_question(client, school["teacher"], subject="history", topic="rome")

    a = client.post(ATTEMPTS, json={"subject": "math", "size": 2}, headers=school["student"]).json()
    assert a["total"] == 2
    assert all(i["question"]["subject"] == "mathematics" for i in a["items"])
    assert len({i["question"]["id"] for i in a["items"]}) == 2

    none = client.post(ATTEMPTS, json={"subject": "chemistry"}, headers=school["student"])
    assert none.status_code == 404
    assert client.post(ATTEMPTS, json={"size": 0}, headers=school["student"]).status_code == 422


def test_submit_rejects_foreign_question_or_choice(client, school):
    q1 = create_question(client, school["teacher"], n=1)
    q2 = create_question(client, school["teacher"], subject="english", topic="grammar", n=2)
    a = client.post(ATTEMPTS, json={"subject": "math"}, headers=school["student"]).json()

    not_in_attempt = client.post(
        f"{ATTEMPTS}/{a['id']}/submit",
        json={"answers": [{"question_id": q2["id"], "choice_id": q2["choices"][0]["id"]}]},
        headers=school["student"],
    )
    assert not_in_attempt.status_code == 400

    wrong_choice = client.post(
        f"{ATTEMPTS}/{a['id']}/submit",
        json={"answers": [{"question_id": q1["id"], "choice_id": q2["choices"][0]["id"]}]},
        headers=school["student"],
    )
    assert wrong_choice.status_code == 400

    dup = client.post(
        f"{ATTEMPTS}/{a['id']}/submit",
        json={"answers": [{"question_id": q1["id"], "choice_id": q1["choices"][0]["id"]}] * 2},
        headers=school["student"],
    )
    assert dup.status_code == 422

    # Failed submissions don't consume the attempt.
    ok = client.post(
        f"{ATTEMPTS}/{a['id']}/submit",
        json={"answers": [{"question_id": q1["id"], "choice_id": q1["choices"][0]["id"]}]},
        headers=school["student"],
    )
    assert ok.status_code == 200 and ok.json()["score"] == 1


def test_attempts_are_private(client, school):
    create_question(client, school["teacher"])
    a = client.post(ATTEMPTS, json={}, headers=school["student"]).json()
    from tests.conftest import register_student

    classmate = register_student(client, school["code"], "classmate@school.edu")
    assert client.get(f"{ATTEMPTS}/{a['id']}", headers=classmate).status_code == 404
    assert client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": []}, headers=classmate).status_code == 404
    assert client.get(ATTEMPTS, headers=classmate).json() == []


def test_topic_stats_weakest_first(client, school):
    teacher, student = school["teacher"], school["student"]
    alg = [create_question(client, teacher, subject="math", topic="algebra", n=n) for n in range(2)]
    geo = [create_question(client, teacher, subject="math", topic="geometry", n=n + 10) for n in range(2)]

    def run(questions, correct: list[bool]):
        topic = questions[0]["topic"]
        a = client.post(ATTEMPTS, json={"topic": topic}, headers=student).json()
        by_id = {q["id"]: q for q in questions}
        answers = []
        for item, ok in zip(a["items"], correct):
            q = by_id[item["question"]["id"]]
            choice = next(c for c in q["choices"] if c["is_correct"] == ok)
            answers.append({"question_id": q["id"], "choice_id": choice["id"]})
        client.post(f"{ATTEMPTS}/{a['id']}/submit", json={"answers": answers}, headers=student)

    run(alg, [True, True])
    run(geo, [True, False])
    # An unsubmitted attempt doesn't count.
    client.post(ATTEMPTS, json={"topic": "geometry"}, headers=student)

    stats = client.get("/api/v1/users/me/stats", headers=student).json()
    assert [(s["topic"], s["answered"], s["correct"], s["accuracy"]) for s in stats] == [
        ("geometry", 2, 1, 0.5),
        ("algebra", 2, 2, 1.0),
    ]
