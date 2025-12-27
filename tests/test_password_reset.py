"""Forgotten passwords: the web app asks for a reset token over an internal call and emails it; the
person follows the link and sets a new password."""

import fakeredis
import pytest

from app.core import redis as redis_module
from app.core.config import get_settings
from app.core.security import create_password_reset_token
from tests.conftest import PASSWORD, auth, create_org

REQUEST = "/api/v1/auth/password-reset/request"
CHECK = "/api/v1/auth/password-reset/check"
CONFIRM = "/api/v1/auth/password-reset/confirm"
KEY = "internal-test-key-0123456789abcdef"
NEW = "brand-new-pass-42"


@pytest.fixture(autouse=True)
def internal_key(app, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_api_key", KEY)


@pytest.fixture
def fake_redis(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", "redis://fake")
    redis_module._client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    yield
    redis_module._client = None


def request(client, email, key=KEY, ip=None):
    headers = {"X-Internal-Key": key} if key else {}
    if ip:
        headers["X-Client-IP"] = ip
    return client.post(REQUEST, json={"email": email}, headers=headers)


def login(client, email, password):
    return client.post("/api/v1/auth/login", data={"username": email, "password": password})


def token_for(client, email="admin@school.edu"):
    r = request(client, email)
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------- only the web app can ask for a token ----------


@pytest.mark.parametrize("key", [None, "wrong-key", KEY[:-1]])
def test_without_the_internal_key_the_route_does_not_exist(client, school, key):
    assert request(client, "admin@school.edu", key=key).status_code == 404


def test_with_no_key_configured_nobody_can_ask(client, school, monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_api_key", None)
    assert request(client, "admin@school.edu").status_code == 404


def test_the_route_is_left_out_of_the_api_docs(client):
    assert "/api/v1/auth/password-reset/request" not in client.get("/openapi.json").json()["paths"]


# ---------- issuing ----------


def test_a_known_email_gets_a_token_and_the_name_for_the_email(client, school):
    body = request(client, "admin@school.edu").json()
    assert body["token"]
    assert (body["email"], body["full_name"]) == ("admin@school.edu", "Ada Admin")


def test_the_email_is_matched_case_insensitively(client, school):
    assert request(client, "  ADMIN@School.EDU ").json()["token"]


def test_an_unknown_email_gets_no_token(client, school):
    assert request(client, "nobody@school.edu").json() == {"token": None, "email": None, "full_name": None}


def test_a_deactivated_account_gets_no_token(client, school, database_url):
    import asyncio

    from sqlalchemy import pool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def deactivate():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE users SET is_active = false WHERE email = 'admin@school.edu'"))
        await engine.dispose()

    asyncio.run(deactivate())
    assert request(client, "admin@school.edu").json()["token"] is None


# ---------- using the link ----------


def test_a_valid_link_checks_out(client, school):
    assert client.post(CHECK, json={"token": token_for(client)}).status_code == 204


def test_resetting_changes_the_password(client, school):
    assert client.post(CONFIRM, json={"token": token_for(client), "new_password": NEW}).status_code == 204
    assert login(client, "admin@school.edu", NEW).status_code == 200
    assert login(client, "admin@school.edu", PASSWORD).status_code == 401


def test_a_link_works_only_once(client, school):
    token = token_for(client)
    assert client.post(CONFIRM, json={"token": token, "new_password": NEW}).status_code == 204
    again = client.post(CONFIRM, json={"token": token, "new_password": "another-pass-99"})
    assert again.status_code == 400
    assert "invalid or has expired" in again.json()["detail"]
    assert client.post(CHECK, json={"token": token}).status_code == 400


def test_every_older_link_dies_once_one_is_used(client, school):
    first, second = token_for(client), token_for(client)
    client.post(CONFIRM, json={"token": second, "new_password": NEW})
    assert client.post(CONFIRM, json={"token": first, "new_password": "another-pass-99"}).status_code == 400


def test_an_expired_link_is_refused(client, school, database_url):
    import asyncio

    from sqlalchemy import pool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def account():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            row = (await conn.execute(text("SELECT id, hashed_password FROM users WHERE email = 'admin@school.edu'"))).one()
        await engine.dispose()
        return row

    user_id, hashed = asyncio.run(account())
    expired = create_password_reset_token(user_id, hashed, ttl_minutes=-1)
    assert client.post(CONFIRM, json={"token": expired, "new_password": NEW}).status_code == 400
    assert login(client, "admin@school.edu", PASSWORD).status_code == 200


def test_a_tampered_link_is_refused(client, school):
    token = token_for(client)
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert client.post(CONFIRM, json={"token": forged, "new_password": NEW}).status_code == 400


def test_a_reset_token_cannot_be_used_to_sign_in(client, school):
    token = token_for(client)
    assert client.get("/api/v1/users/me", headers=auth(token)).status_code == 401


def test_a_sign_in_token_cannot_reset_a_password(client, school):
    session = school["admin"]["Authorization"].removeprefix("Bearer ")
    assert client.post(CONFIRM, json={"token": session, "new_password": NEW}).status_code == 400


def test_the_new_password_follows_the_usual_rules(client, school):
    assert client.post(CONFIRM, json={"token": token_for(client), "new_password": "short"}).status_code == 422


def test_a_reset_is_recorded_in_the_audit_log(client, school, database_url):
    import asyncio

    from sqlalchemy import pool, text
    from sqlalchemy.ext.asyncio import create_async_engine

    client.post(CONFIRM, json={"token": token_for(client), "new_password": NEW})

    async def actions():
        engine = create_async_engine(database_url, poolclass=pool.NullPool)
        async with engine.begin() as conn:
            rows = (await conn.execute(text("SELECT action, actor_email FROM audit_log WHERE action = 'user.password_reset'"))).all()
        await engine.dispose()
        return rows

    assert [tuple(r) for r in asyncio.run(actions())] == [("user.password_reset", "admin@school.edu")]


# ---------- rate limits ----------


def test_requests_are_limited_per_email(client, school, fake_redis, monkeypatch):
    monkeypatch.setattr(get_settings(), "password_resets_per_email_per_hour", 2)
    assert request(client, "admin@school.edu").status_code == 200
    assert request(client, "admin@school.edu").status_code == 200
    assert request(client, "admin@school.edu").status_code == 429
    assert request(client, "teach@school.edu").status_code == 200, "another account is unaffected"


def test_unknown_emails_count_too_so_the_limit_reveals_nothing(client, school, fake_redis, monkeypatch):
    monkeypatch.setattr(get_settings(), "password_resets_per_email_per_hour", 1)
    assert request(client, "ghost@school.edu").status_code == 200
    assert request(client, "ghost@school.edu").status_code == 429


def test_requests_are_limited_per_ip(client, school, fake_redis, monkeypatch):
    monkeypatch.setattr(get_settings(), "password_resets_per_ip_per_hour", 2)
    create_org(client, email="admin@other.edu", name="Other High")
    assert request(client, "admin@school.edu", ip="203.0.113.7").status_code == 200
    assert request(client, "teach@school.edu", ip="203.0.113.7").status_code == 200
    assert request(client, "admin@other.edu", ip="203.0.113.7").status_code == 429
    assert request(client, "admin@other.edu", ip="198.51.100.4").status_code == 200
