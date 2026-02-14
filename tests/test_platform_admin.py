import pytest

from tests.conftest import PASSWORD, create_org, create_question, join_code, question_payload, register_student

METRICS = "/api/v1/platform/metrics"
IMPORT = "/api/v1/platform/questions/import"


def library_payload(n: int, grade: int = 9, subject="math", topic="algebra") -> dict:
    return {**question_payload(subject=subject, topic=topic, n=n), "grade": grade, "source": "Past exam 2023"}


# ---------- access control ----------


def test_platform_routes_hidden_from_school_users(client, school):
    for headers in (school["admin"], school["teacher"], school["student"]):
        assert client.get(METRICS, headers=headers).status_code == 404
        assert client.post(IMPORT, json={"questions": [library_payload(1)]}, headers=headers).status_code == 404
        assert client.post("/api/v1/platform/announcements", json={"title": "x", "body": "y"}, headers=headers).status_code == 404
    assert client.get("/api/v1/users/me", headers=school["admin"]).json()["is_platform_admin"] is False


def test_platform_admin_profile(client, platform_admin):
    me = client.get("/api/v1/users/me", headers=platform_admin).json()
    assert me["is_platform_admin"] is True and me["role"] == "admin"


def test_cannot_join_internal_workspace(client, platform_admin):
    hq_code = client.get("/api/v1/organizations/me", headers=platform_admin).json()["join_code"]
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": hq_code, "full_name": "Sneaky", "email": "sneaky@x.io", "password": PASSWORD},
    )
    assert r.status_code == 400


# ---------- shared library ----------


def test_library_import_is_visible_to_every_school(client, school, platform_admin):
    r = client.post(IMPORT, json={"questions": [library_payload(1, grade=9), library_payload(2, grade=12)]}, headers=platform_admin)
    assert r.status_code == 201, r.text
    assert (r.json()["created"], r.json()["possible_duplicates"]) == (2, 0)

    other = create_org(client, email="boss@other.edu", name="Other")
    for headers in (school["student"], other):
        listed = client.get("/api/v1/questions", params={"scope": "library"}, headers=headers).json()
        assert len(listed) == 2
        assert all(q["is_library"] and q["source"] == "Past exam 2023" for q in listed)
        assert all(set(c) == {"id", "text"} for q in listed for c in q["choices"])

    grade12 = client.get("/api/v1/questions", params={"grade": 12}, headers=school["student"]).json()
    assert [q["grade"] for q in grade12] == [12]


def test_library_and_school_scopes(client, school, platform_admin):
    client.post(IMPORT, json={"questions": [library_payload(1)]}, headers=platform_admin)
    own = create_question(client, school["teacher"], n=5)
    assert own["is_library"] is False

    def ids(scope):
        return {q["id"] for q in client.get("/api/v1/questions", params={"scope": scope}, headers=school["student"]).json()}

    assert ids("school") == {own["id"]}
    assert len(ids("library")) == 1
    assert ids("all") == ids("school") | ids("library")

    other = create_org(client, email="boss@other.edu", name="Other")
    assert own["id"] not in {q["id"] for q in client.get("/api/v1/questions", headers=other).json()}


def test_invalid_import_is_rejected_atomically(client, platform_admin, school):
    bad = library_payload(2)
    bad["choices"] = [{"text": "a"}, {"text": "b"}]
    r = client.post(IMPORT, json={"questions": [library_payload(1), bad]}, headers=platform_admin)
    assert r.status_code == 422
    assert client.get("/api/v1/questions", params={"scope": "library"}, headers=school["student"]).json() == []


def test_import_rejects_out_of_range_grade(client, platform_admin):
    r = client.post(IMPORT, json={"questions": [library_payload(1, grade=13)]}, headers=platform_admin)
    assert r.status_code == 422


def test_students_practice_library_questions_and_grade_filter(client, school, platform_admin):
    client.post(IMPORT, json={"questions": [library_payload(1, grade=9), library_payload(2, grade=11)]}, headers=platform_admin)
    a = client.post("/api/v1/attempts", json={"mode": "adaptive", "grade": 11}, headers=school["student"]).json()
    assert [i["question"]["grade"] for i in a["items"]] == [11]

    nxt = client.get("/api/v1/learning/next-question", params={"grade": 9}, headers=school["student"]).json()
    assert nxt["question"]["grade"] == 9 and nxt["question"]["is_library"] is True

    # Answer keys for library questions are visible to staff, not students.
    qid = a["items"][0]["question"]["id"]
    assert "explanation" in client.get(f"/api/v1/questions/{qid}", headers=school["teacher"]).json()
    assert "explanation" not in client.get(f"/api/v1/questions/{qid}", headers=school["student"]).json()


def test_single_library_question(client, platform_admin):
    r = client.post("/api/v1/platform/questions", json=library_payload(3, grade=10), headers=platform_admin)
    assert r.status_code == 201
    body = r.json()
    assert body["is_library"] is True and body["grade"] == 10 and body["source"] == "Past exam 2023"


# ---------- teacher invite codes ----------


def test_teacher_code_only_for_admins_and_creates_teachers(client, school):
    org = client.get("/api/v1/organizations/me", headers=school["admin"]).json()
    assert len(org["teacher_code"]) == 8 and org["teacher_code"] != org["join_code"]
    assert "teacher_code" not in client.get("/api/v1/organizations/me", headers=school["teacher"]).json()

    new_teacher = register_student(client, org["teacher_code"].lower(), "newteacher@school.edu")
    assert client.get("/api/v1/users/me", headers=new_teacher).json()["role"] == "teacher"


def test_rotate_teacher_code_leaves_student_code(client, school):
    before = client.get("/api/v1/organizations/me", headers=school["admin"]).json()
    after = client.post("/api/v1/organizations/me/join-code", params={"kind": "teacher"}, headers=school["admin"]).json()
    assert after["teacher_code"] != before["teacher_code"]
    assert after["join_code"] == before["join_code"]
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": before["teacher_code"], "full_name": "Late", "email": "late@school.edu", "password": PASSWORD},
    )
    assert r.status_code == 400


# ---------- metrics ----------


def _answer_all_correct(client, headers, attempt, bank):
    answers = [
        {"question_id": i["question"]["id"], "choice_id": next(c["id"] for c in bank[i["question"]["id"]]["choices"] if c["is_correct"])}
        for i in attempt["items"]
    ]
    r = client.post(f"/api/v1/attempts/{attempt['id']}/submit", json={"answers": answers}, headers=headers)
    assert r.status_code == 200


def test_metrics(client, school, platform_admin):
    client.post(IMPORT, json={"questions": [library_payload(1, subject="physics", topic="motion")]}, headers=platform_admin)
    q = create_question(client, school["teacher"], n=7)
    bank = {q["id"]: q}

    second = create_org(client, email="boss@other.edu", name="Other School")
    register_student(client, join_code(client, second), "kid@other.edu")

    a = client.post("/api/v1/attempts", json={"subject": "math"}, headers=school["student"]).json()
    _answer_all_correct(client, school["student"], a, bank)
    # Unsubmitted attempts don't count.
    client.post("/api/v1/attempts", json={"subject": "math"}, headers=school["student"])

    m = client.get(METRICS, headers=platform_admin).json()
    t = m["totals"]
    assert t["schools"] == 2
    assert t["users"] == {"students": 2, "teachers": 1, "admins": 2}
    assert (t["library_questions"], t["school_questions"]) == (1, 1)
    assert (t["attempts_submitted"], t["answers_graded"], t["accuracy"]) == (1, 1, 1.0)

    act = m["activity"]
    assert act["new_schools_30d"] == 2 and act["new_students_30d"] == 2
    assert act["active_students_7d"] == act["active_students_30d"] == 1
    assert act["attempts_7d"] == act["attempts_30d"] == 1

    assert len(m["daily"]) == 30
    today = m["daily"][-1]
    assert today["attempts"] == 1
    assert today["signups"] == 5  # 2 admins + teacher + 2 students; the platform admin is excluded

    rows = {s["name"]: s for s in m["schools"]}
    assert set(rows) == {"Springfield High", "Other School"}
    sp = rows["Springfield High"]
    assert (sp["students"], sp["teachers"], sp["own_questions"], sp["attempts"], sp["active_students_30d"]) == (1, 1, 1, 1, 1)
    assert sp["last_activity"] is not None
    assert rows["Other School"]["attempts"] == 0 and rows["Other School"]["last_activity"] is None

    assert m["subjects"] == [{"subject": "mathematics", "answered": 1, "accuracy": 1.0}]


def test_metrics_empty_platform(client, platform_admin):
    m = client.get(METRICS, headers=platform_admin).json()
    assert m["totals"]["schools"] == 0
    assert m["totals"]["accuracy"] is None
    assert m["schools"] == [] and m["subjects"] == []
    assert sum(d["attempts"] for d in m["daily"]) == 0


@pytest.mark.parametrize("path", [METRICS])
def test_metrics_requires_auth(client, path):
    assert client.get(path).status_code == 401
