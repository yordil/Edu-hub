"""Password reset: issue a short-lived link token, check it, and set the new password.

The API never sends the email. It hands the token to the web app over an authenticated
server-to-server call, and the web app emails it. Nothing here tells a caller whether an email
address has an account.
"""

import hashlib
import logging
from dataclasses import dataclass

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import TooManyRequests, ValidationFailed
from app.core.redis import get_redis
from app.core.security import (
    create_password_reset_token,
    decode_password_reset_token,
    hash_password,
    password_fingerprint,
)
from app.models.user import User
from app.services import audit_service

log = logging.getLogger("eduhub.password")

INVALID_LINK = "This reset link is invalid or has expired. Request a new one."


@dataclass(frozen=True)
class IssuedReset:
    token: str
    user: User


async def _within_limit(key: str, limit: int) -> bool:
    """Count one request against `key` for an hour. Fails open: if Redis is down, a student locked out of
    their account matters more than a few extra emails."""
    redis = get_redis()
    if redis is None:
        return True
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 3600)
        return count <= limit
    except Exception as e:  # noqa: BLE001
        log.warning("password reset rate limit unavailable: %s", e)
        return True


async def issue_reset(db: AsyncSession, email: str, client_ip: str | None) -> IssuedReset | None:
    """A reset token for this email, or None when there is no active account for it.

    Every request is counted before the lookup, so the limits behave the same whether or not the
    account exists — otherwise they would reveal which emails are registered.
    """
    s = get_settings()
    email = email.strip().lower()
    by_email = await _within_limit(f"pwreset:email:{hashlib.sha256(email.encode()).hexdigest()}", s.password_resets_per_email_per_hour)
    by_ip = client_ip is None or await _within_limit(f"pwreset:ip:{client_ip}", s.password_resets_per_ip_per_hour)
    if not (by_email and by_ip):
        raise TooManyRequests("Too many reset requests. Try again in an hour.")

    user = await db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        return None
    return IssuedReset(create_password_reset_token(user.id, user.hashed_password, s.password_reset_ttl_minutes), user)


async def _user_for(db: AsyncSession, token: str) -> User:
    try:
        user_id, fingerprint = decode_password_reset_token(token)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise ValidationFailed(INVALID_LINK) from None
    user = await db.get(User, user_id)
    # A changed fingerprint means the password has changed since the link was sent: already used.
    if user is None or not user.is_active or password_fingerprint(user.hashed_password) != fingerprint:
        raise ValidationFailed(INVALID_LINK)
    return user


async def check_reset(db: AsyncSession, token: str) -> None:
    """Lets the reset page say "expired" before the student types a new password."""
    await _user_for(db, token)


async def confirm_reset(db: AsyncSession, token: str, new_password: str) -> None:
    user = await _user_for(db, token)
    user.hashed_password = hash_password(new_password)
    audit_service.record(db, user, "user.password_reset", "user", user.id)
    await db.commit()
