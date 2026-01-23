import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_access_token
from app.models.user import Role, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(db: DbSession, token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        user_id = uuid.UUID(decode_access_token(token)["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized from None
    # Load from DB on every request so role changes and deactivation apply immediately.
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role):
    async def _check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _check


StaffUser = Annotated[User, Depends(require_roles(Role.teacher, Role.admin))]
AdminUser = Annotated[User, Depends(require_roles(Role.admin))]


async def _require_platform_admin(user: CurrentUser) -> User:
    if not user.is_platform_admin:
        # 404 keeps the operator console invisible to school users.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return user


PlatformAdmin = Annotated[User, Depends(_require_platform_admin)]


async def _require_active_plan(user: CurrentUser, db: DbSession) -> User:
    from app.services.billing_service import get_active_subscription

    sub = await get_active_subscription(db, user.org_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Active subscription required. Upgrade your plan to access this feature.",
        )
    return user


ActivePlanUser = Annotated[User, Depends(_require_active_plan)]


async def _require_tutor_access(user: CurrentUser, db: DbSession) -> User:
    """A paid plan, or a school still inside its one-time free trial."""
    from app.ai.tutor import tutor_access

    access = await tutor_access(db, user.org_id)
    if access.allowed:
        return user
    if access.trial_allowance == 0:
        detail = "Active subscription required. Upgrade your plan to access this feature."
    else:
        detail = (
            f"Your school has used its free AI tutor trial ({access.trial_allowance:,} tokens). "
            "Upgrade to Pro to keep using the tutor."
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


TutorUser = Annotated[User, Depends(_require_tutor_access)]


class Page:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        self.limit = limit
        self.offset = offset


Pagination = Annotated[Page, Depends()]
