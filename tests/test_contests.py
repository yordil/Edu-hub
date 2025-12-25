from datetime import datetime, timedelta, timezone

import pytest

from app.services import contest_service
from tests.conftest import PASSWORD, create_org, create_question, join_code, question_payload, register_student

URL = "/api/v1/contests"


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(contest_service, "_now", lambda: c.now)
    return c


@pytest.fixture
def bank(client, school):
    math = [create_question(client, school["teacher"], subject="mathematics", topic="algebra", n=i) for i in range(4)]
    physics = create_question(client, school["teacher"], subject="physics", topic="motion", n=10)
    history = create_question(client, school["teacher"], subject="history", topic="ww2", n=11)
    return {q["id"]: q for q in [*math, physics, history]}


def window(start_min=-1, end_min=60):
    now = datetime.now(timezone.utc)
    return {"starts_at": (now + timedelta(minutes=start_min)).isoformat(), "ends_at": (now + timedelta(minutes=end_min)).isoformat()}


def create(client, headers, **kw):
    body = {"title": "Friday Math Sprint", "subject": "mathematics", "duration_minutes": 10, "question_count": 3, **window(), **kw}
    r = client.post(URL, json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def answers_for(play, bank, correct: int):
    """Answer the first `correct` questions right and the rest wrong."""
    out = []
    for i, q in enumerate(play["questions"]):
        want = i < correct
        out.append({"question_id": q["id"], "choice_id": next(c["id"] for c in bank[q["id"]]["choices"] if c["is_correct"] == want)})
    return out


def register(client, code, email, stream=None):
    body = {"join_code": code, "full_name": f"Kid {email.split('@')[0].title()} Doe", "email": email, "password": PASSWORD}
    if stream:
        body["stream"] = stream
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------- creating ----------


def test_teacher_creates_contest_with_fixed_questions(client, school, bank):
    c = create(client, school["teacher"])
    assert (c["scope"], c["status"], c["question_count"], c["participants"]) == ("school", "live", 3, 0)
    assert c["my_entry"] is None
    student_view = client.get(f"{URL}/{c['id']}", headers=school["student"]).json()
    assert student_view["can_join"] is True


def test_students_cannot_create(client, school, bank):
    assert client.post(URL, json={"title": "Mine", **window()}, headers=school["student"]).status_code == 403


def test_not_enough_questions(client, school, bank):
    r = client.post(URL, json={"title": "Too big", "subject": "physics", "question_count": 5, **window()}, headers=school["teacher"])
    assert r.status_code == 400 and r.json()["detail"].startswith("Only 1 question(s)")


@pytest.mark.parametrize(
    "bad",
    [
        {"subject": "algorithms"},
        {"stream": "social", "subject": "physics"},
        {"duration_minutes": 90},  # longer than the 61-minute window
        window(start_min=-120, end_min=-60),  # already over
        {"question_count": 2},
    ],
)
def test_contest_validation(client, school, bank, bad):
    body = {"title": "Bad", "duration_minutes": 10, "question_count": 3, **window(), **bad}
    assert client.post(URL, json=body, headers=school["teacher"]).status_code == 422


# ---------- playing ----------


def test_full_contest_flow(client, school, bank, clock):
    c = create(client, school["teacher"])
    play = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    assert len(play["questions"]) == 3
    assert all(set(ch) == {"id", "text"} for q in play["questions"] for ch in q["choices"])
    assert all(q["subject"] == "mathematics" for q in play["questions"])

    # Refreshing resumes the same entry and timer.
    clock.advance(seconds=30)
    again = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    assert again["entry"]["started_at"] == play["entry"]["started_at"]
    assert client.get(f"{URL}/{c['id']}/play", headers=school["student"]).status_code == 200

    clock.advance(seconds=60)
    r = client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(play, bank, 2)}, headers=school["student"])
    assert r.status_code == 200, r.text
    entry = r.json()["my_entry"]
    assert (entry["score"], entry["total"], entry["rank"], entry["time_taken_seconds"]) == (2, 3, 1, 90)

    assert client.post(f"{URL}/{c['id']}/submit", json={"answers": []}, headers=school["student"]).status_code == 409
    assert client.post(f"{URL}/{c['id']}/start", headers=school["student"]).status_code == 409
    assert client.get(f"{URL}/{c['id']}/play", headers=school["student"]).status_code == 404


def test_cannot_join_before_start_or_after_end(client, school, bank, clock):
    upcoming = create(client, school["teacher"], **window(start_min=30, end_min=90))
    assert upcoming["status"] == "upcoming"
    assert client.post(f"{URL}/{upcoming['id']}/start", headers=school["student"]).json()["detail"] == "This contest hasn't started yet"

    live = create(client, school["teacher"])
    clock.advance(hours=2)
    assert client.post(f"{URL}/{live['id']}/start", headers=school["student"]).json()["detail"] == "This contest has ended"


def test_late_submission_is_rejected_and_scores_zero(client, school, bank, clock):
    c = create(client, school["teacher"])
    play = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    clock.advance(minutes=11)
    r = client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(play, bank, 3)}, headers=school["student"])
    assert r.status_code == 409 and "Time is up" in r.json()["detail"]
    entry = client.get(f"{URL}/{c['id']}", headers=school["student"]).json()["my_entry"]
    assert entry["score"] == 0 and entry["time_taken_seconds"] == 600


def test_submit_within_grace_period_counts(client, school, bank, clock):
    c = create(client, school["teacher"])
    play = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    clock.advance(minutes=10, seconds=10)
    r = client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(play, bank, 3)}, headers=school["student"])
    assert r.status_code == 200
    assert r.json()["my_entry"]["score"] == 3 and r.json()["my_entry"]["time_taken_seconds"] == 600


def test_submit_rejects_foreign_question(client, school, bank, clock):
    c = create(client, school["teacher"])
    client.post(f"{URL}/{c['id']}/start", headers=school["student"])
    physics = next(q for q in bank.values() if q["subject"] == "physics")
    r = client.post(
        f"{URL}/{c['id']}/submit",
        json={"answers": [{"question_id": physics["id"], "choice_id": physics["choices"][0]["id"]}]},
        headers=school["student"],
    )
    assert r.status_code == 400
    assert client.post(f"{URL}/{c['id']}/submit", json={"answers": []}, headers=school["teacher"]).status_code == 404


def test_stream_restricted_contest(client, school, bank):
    c = create(client, school["teacher"], subject=None, stream="natural")
    social = register(client, school["code"], "soc@school.edu", "social")
    natural = register(client, school["code"], "nat@school.edu", "natural")
    assert client.get(f"{URL}/{c['id']}", headers=social).json()["can_join"] is False
    assert client.post(f"{URL}/{c['id']}/start", headers=social).status_code == 403
    play = client.post(f"{URL}/{c['id']}/start", headers=natural).json()
    assert {q["subject"] for q in play["questions"]} <= {"mathematics", "physics"}


# ---------- leaderboards ----------


def test_leaderboard_ranks_by_score_then_time(client, school, bank, clock):
    c = create(client, school["teacher"])
    alice = register(client, school["code"], "alice@school.edu")
    bob = register(client, school["code"], "bob@school.edu")
    plays = {name: client.post(f"{URL}/{c['id']}/start", headers=h).json() for name, h in [("me", school["student"]), ("alice", alice), ("bob", bob)]}

    clock.advance(seconds=30)
    client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(plays["alice"], bank, 2)}, headers=alice)
    clock.advance(seconds=30)
    client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(plays["bob"], bank, 2)}, headers=bob)
    client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(plays["me"], bank, 3)}, headers=school["student"])

    board = client.get(f"{URL}/{c['id']}/leaderboard", headers=bob).json()
    assert board["participants"] == 3
    assert [(r["rank"], r["name"], r["score"], r["time_taken_seconds"]) for r in board["rows"]] == [
        (1, "Sam Student", 3, 60),
        (2, "Kid Alice Doe", 2, 30),
        (3, "Kid Bob Doe", 2, 60),
    ]
    assert board["me"]["rank"] == 3 and board["me"]["is_me"] is True
    assert all(r["school"] == "Springfield High" for r in board["rows"])


def test_school_contests_are_private(client, school, bank):
    c = create(client, school["teacher"])
    other = create_org(client, email="boss@other.edu", name="Other")
    assert client.get(URL, headers=other).json() == []
    assert client.get(f"{URL}/{c['id']}", headers=other).status_code == 404
    assert client.get(f"{URL}/{c['id']}/leaderboard", headers=other).status_code == 404


def test_platform_contest_across_schools(client, school, platform_admin, clock):
    library = [{**question_payload(subject="mathematics", n=i), "grade": 10} for i in range(3)]
    library.append({**question_payload(subject="physics", n=9), "grade": 10})
    client.post("/api/v1/platform/questions/import", json={"questions": library}, headers=platform_admin)

    assert client.post("/api/v1/platform/contests", json={"title": "x", **window()}, headers=school["teacher"]).status_code == 404
    r = client.post(
        "/api/v1/platform/contests",
        json={"title": "EduHub Weekly #1", "duration_minutes": 10, "question_count": 3, **window()},
        headers=platform_admin,
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["scope"] == "platform"

    other_admin = create_org(client, email="boss@other.edu", name="Riverside")
    rival = register_student(client, join_code(client, other_admin), "rival@riverside.edu")
    for headers in (school["student"], rival):
        play = client.post(f"{URL}/{c['id']}/start", headers=headers).json()
        # Open to both streams: only subjects everyone studies.
        assert {q["subject"] for q in play["questions"]} == {"mathematics"}
        client.post(f"{URL}/{c['id']}/submit", json={"answers": []}, headers=headers)

    rows = client.get(f"{URL}/{c['id']}/leaderboard", headers=rival).json()["rows"]
    assert {(r["name"], r["school"]) for r in rows} == {("Sam S.", "Springfield High"), ("Sam S.", "Riverside")}


def test_list_orders_live_upcoming_ended(client, school, bank, clock):
    ended = create(client, school["teacher"], title="Ended one", **window(start_min=-1, end_min=11))
    upcoming = create(client, school["teacher"], title="Upcoming one", **window(start_min=120, end_min=180))
    live = create(client, school["teacher"], title="Live one", **window(start_min=-1, end_min=300))
    clock.advance(minutes=30)
    listed = client.get(URL, headers=school["student"]).json()
    assert [(c["title"], c["status"]) for c in listed] == [("Live one", "live"), ("Upcoming one", "upcoming"), ("Ended one", "ended")]
    assert {live["id"], upcoming["id"], ended["id"]} == {c["id"] for c in listed}


def test_review_only_after_contest_ends(client, school, bank, clock):
    c = create(client, school["teacher"])
    play = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(play, bank, 1)}, headers=school["student"])
    assert client.get(f"{URL}/{c['id']}/review", headers=school["student"]).status_code == 409

    clock.advance(hours=2)
    review = client.get(f"{URL}/{c['id']}/review", headers=school["student"]).json()
    assert [i["is_correct"] for i in review["items"]] == [True, False, False]
    assert all(i["correct_choice_id"] and i["explanation"] for i in review["items"])


# ---------- staff host, students compete ----------


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_staff_host_contests_rather_than_enter_them(client, school, bank, role):
    """A teacher's perfect score would top the student leaderboard; staff watch instead."""
    c = create(client, school["teacher"])
    staff = school[role]

    assert client.get(f"{URL}/{c['id']}", headers=staff).json()["can_join"] is False
    r = client.post(f"{URL}/{c['id']}/start", headers=staff)
    assert r.status_code == 403
    assert "host contests rather than take part" in r.json()["detail"]


def test_staff_still_follow_the_live_leaderboard(client, school, bank):
    c = create(client, school["teacher"])
    play = client.post(f"{URL}/{c['id']}/start", headers=school["student"]).json()
    client.post(f"{URL}/{c['id']}/submit", json={"answers": answers_for(play, bank, 2)}, headers=school["student"])

    board = client.get(f"{URL}/{c['id']}/leaderboard", headers=school["teacher"])
    assert board.status_code == 200
    rows = board.json()["rows"]
    assert [(r["rank"], r["score"], r["is_me"]) for r in rows] == [(1, 2, False)]
    assert board.json()["me"] is None, "the host has no row of their own"
