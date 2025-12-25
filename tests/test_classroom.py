from tests.conftest import create_org, join_code, make_teacher, register_student
from tests.test_tutor import practice_with_mistake

OVERVIEW = "/api/v1/classroom/overview"


def test_class_overview(client, school):
    practice_with_mistake(client, school)  # Sam: algebra right, genetics wrong
    register_student(client, school["code"], "quiet@school.edu")

    assert client.get(OVERVIEW, headers=school["student"]).status_code == 403
    o = client.get(OVERVIEW, headers=school["teacher"]).json()
    assert (o["students"], o["active_7d"], o["questions_7d"], o["accuracy"]) == (2, 1, 2, 0.5)

    rows = {r["email"]: r for r in o["rows"]}
    assert set(rows) == {"stu@school.edu", "quiet@school.edu"}  # staff aren't listed
    sam = rows["stu@school.edu"]
    assert (sam["questions_total"], sam["questions_7d"], sam["accuracy"], sam["weakest_topic"], sam["current_streak"]) == (2, 2, 0.5, "genetics", 1)
    assert sam["last_active"] is not None and 0 < sam["avg_mastery"] < 1
    quiet = rows["quiet@school.edu"]
    assert (quiet["questions_total"], quiet["accuracy"], quiet["avg_mastery"], quiet["last_active"]) == (0, None, None, None)

    assert [t["topic"] for t in o["topics"]] == ["genetics", "algebra"]
    assert all(t["students"] == 1 for t in o["topics"])


def test_overview_is_school_scoped(client, school):
    practice_with_mistake(client, school)
    other = create_org(client, email="boss@other.edu", name="Other")
    o = client.get(OVERVIEW, headers=other).json()
    assert (o["students"], o["rows"], o["topics"]) == (0, [], [])


def test_student_detail(client, school):
    practice_with_mistake(client, school)
    sam_id = client.get("/api/v1/users/me", headers=school["student"]).json()["id"]

    d = client.get(f"/api/v1/classroom/students/{sam_id}", headers=school["admin"]).json()
    assert d["student"]["email"] == "stu@school.edu"
    assert [m["topic"] for m in d["mastery"]] == ["genetics", "algebra"]
    assert len(d["attempts"]) == 1 and d["attempts"][0]["score"] == 1
    assert d["activity"]["current_streak"] == 1 and d["activity"]["total_questions"] == 2

    other = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(f"/api/v1/classroom/students/{sam_id}", headers=other).status_code == 404
    assert client.get(f"/api/v1/classroom/students/{sam_id}", headers=school["student"]).status_code == 403

    teacher_id = client.get("/api/v1/users/me", headers=school["teacher"]).json()["id"]
    assert client.get(f"/api/v1/classroom/students/{teacher_id}", headers=school["admin"]).status_code == 404


def test_weekly_leaderboard(client, school):
    practice_with_mistake(client, school)  # Sam: 1 correct of 2
    newcomer = register_student(client, school["code"], "new@school.edu")

    board = client.get("/api/v1/leaderboard/weekly", headers=newcomer).json()
    assert [(r["rank"], r["name"], r["correct"], r["questions"], r["streak"]) for r in board["rows"]] == [(1, "Sam Student", 1, 2, 1)]
    assert board["me"] is None
    assert client.get("/api/v1/leaderboard/weekly", headers=school["student"]).json()["me"]["is_me"] is True

    other = create_org(client, email="boss@other.edu", name="Other")
    make_teacher(client, other, join_code(client, other), "t@other.edu")
    assert client.get("/api/v1/leaderboard/weekly", headers=other).json()["rows"] == []
