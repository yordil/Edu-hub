"""Real-time contest leaderboards: Redis sorted sets + pub/sub over authenticated WebSockets (fakeredis in tests)."""

from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from starlette.websockets import WebSocketDisconnect

from app.core import redis as redis_module
from app.core.config import get_settings
from app.services.live_service import composite, decompose
from tests.conftest import create_question
from tests.test_contests import register

URL = "/api/v1/contests"


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", "redis://fake")
    server = fakeredis.FakeServer()
    redis_module._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    yield server
    redis_module._client = None


@pytest.fixture
def contest(client, school):
    bank = {}
    for i in range(3):
        qd = create_question(client, school["teacher"], subject="mathematics", topic="algebra", n=i)
        bank[qd["id"]] = qd
    now = datetime.now(timezone.utc)
    c = client.post(
        URL,
        json={"title": "Live cup", "subject": "mathematics", "question_count": 3, "duration_minutes": 10,
              "starts_at": (now - timedelta(minutes=1)).isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat()},
        headers=school["teacher"],
    ).json()
    return c, bank


def play(client, c, bank, headers, correct):
    p = client.post(f"{URL}/{c['id']}/start", headers=headers).json()
    answers = [
        {"question_id": q["id"], "choice_id": next(ch["id"] for ch in bank[q["id"]]["choices"] if ch["is_correct"] == (i < correct))}
        for i, q in enumerate(p["questions"])
    ]
    r = client.post(f"{URL}/{c['id']}/submit", json={"answers": answers}, headers=headers)
    assert r.status_code == 200, r.text


def ticket(client, c, headers):
    r = client.post(f"{URL}/{c['id']}/live-ticket", headers=headers)
    assert r.status_code == 200
    return r.json()["ticket"]


def test_composite_score_orders_by_score_then_time():
    assert composite(3, 120) > composite(2, 5) > composite(2, 60) > composite(0, 0)
    for score, seconds in [(0, 0), (0, 59), (3, 120), (30, 10_799)]:
        assert decompose(composite(score, seconds)) == (score, seconds)


def test_live_board_pushes_updates(client, school, contest, fake_redis):
    c, bank = contest
    rival = register(client, school["code"], "rival@school.edu")
    play(client, c, bank, rival, correct=2)

    with client.websocket_connect(f"{URL}/{c['id']}/live?ticket={ticket(client, c, school['student'])}") as ws:
        first = ws.receive_json()
        assert first["type"] == "board" and first["source"] == "redis"
        assert [(r["name"], r["score"]) for r in first["rows"]] == [("Kid Rival Doe", 2)]
        assert first["me"] is None

        play(client, c, bank, school["student"], correct=3)
        update = ws.receive_json()
        while update["type"] == "ping":
            update = ws.receive_json()
        assert [(r["rank"], r["name"], r["score"]) for r in update["rows"]] == [(1, "Sam Student", 3), (2, "Kid Rival Doe", 2)]
        assert update["me"]["rank"] == 1 and update["participants"] == 2


def test_board_is_rebuilt_from_database_when_redis_is_empty(client, school, contest, fake_redis):
    c, bank = contest
    play(client, c, bank, school["student"], correct=1)
    fake_redis.connected = True
    import asyncio

    asyncio.run(redis_module._client.flushall())
    with client.websocket_connect(f"{URL}/{c['id']}/live?ticket={ticket(client, c, school['student'])}") as ws:
        board = ws.receive_json()
        assert board["source"] == "redis" and board["me"]["score"] == 1


def test_falls_back_to_database_without_redis(client, school, contest):
    c, bank = contest
    play(client, c, bank, school["student"], correct=2)
    with client.websocket_connect(f"{URL}/{c['id']}/live?ticket={ticket(client, c, school['student'])}") as ws:
        board = ws.receive_json()
        assert board["source"] == "database"
        assert board["rows"][0]["score"] == 2 and board["rows"][0]["is_me"] is True


def test_tickets_are_scoped_and_short_lived(client, school, contest, fake_redis):
    c, _ = contest
    access_token = school["student"]["Authorization"].split()[1]

    for bad in ["", "garbage", access_token]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{URL}/{c['id']}/live?ticket={bad}") as ws:
                ws.receive_json()
        assert exc.value.code == 4401

    t = ticket(client, c, school["student"])
    # A ticket is not an access token...
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401
    # ...and only opens the contest it was issued for.
    other = client.post(
        URL,
        json={"title": "Other cup", "subject": "mathematics", "question_count": 3, "duration_minutes": 10,
              "starts_at": datetime.now(timezone.utc).isoformat(), "ends_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        headers=school["teacher"],
    ).json()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"{URL}/{other['id']}/live?ticket={t}") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ticket_requires_access_to_contest(client, school, contest):
    from tests.conftest import create_org

    c, _ = contest
    outsider = create_org(client, email="boss@other.edu", name="Other")
    assert client.post(f"{URL}/{c['id']}/live-ticket", headers=outsider).status_code == 404
