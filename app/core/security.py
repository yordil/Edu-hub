import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings

_JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# Checked against when the email is unknown, so login timing doesn't reveal which emails exist.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def generate_join_code() -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(8))


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("typ", "access") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return payload


def password_fingerprint(hashed_password: str) -> str:
    """Changes whenever the password does (bcrypt re-salts), so a reset token carrying it stops working
    the moment it has been used — single use without storing tokens anywhere."""
    return hashlib.sha256(hashed_password.encode()).hexdigest()[:16]


def create_password_reset_token(user_id: uuid.UUID, hashed_password: str, ttl_minutes: int) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "typ": "reset",
        "fp": password_fingerprint(hashed_password),
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_password_reset_token(token: str) -> tuple[uuid.UUID, str]:
    """(user id, password fingerprint). The `typ` check matters both ways: a reset token can't be used
    to sign in, and a sign-in token can't reset a password."""
    settings = get_settings()
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub", "fp"]})
    if payload.get("typ") != "reset":
        raise jwt.InvalidTokenError("not a password reset token")
    return uuid.UUID(payload["sub"]), payload["fp"]


def create_ws_ticket(user_id: uuid.UUID, contest_id: uuid.UUID) -> str:
    """Browsers can't send auth headers on WebSockets, so they get a 60s ticket scoped to one contest."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "cid": str(contest_id),
        "typ": "ws",
        "iat": now,
        "exp": now + timedelta(seconds=settings.ws_ticket_ttl_seconds),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_ws_ticket(ticket: str, contest_id: uuid.UUID) -> uuid.UUID:
    settings = get_settings()
    payload = jwt.decode(ticket, settings.secret_key, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub", "cid"]})
    if payload.get("typ") != "ws" or payload["cid"] != str(contest_id):
        raise jwt.InvalidTokenError("ticket not valid for this contest")
    return uuid.UUID(payload["sub"])
