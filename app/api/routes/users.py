import uuid

from fastapi import APIRouter

from app.api.deps import AdminUser, CurrentUser, DbSession, Pagination, StaffUser
from app.schemas.quiz import TopicStat
from app.schemas.user import ProfileUpdate, RoleUpdate, UserOut
from app.services import quiz_service, user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(data: ProfileUpdate, user: CurrentUser, db: DbSession):
    """Students set their grade (9-12) and, from grade 11, their stream. Moving from grade 10 to 11
    must name a stream in the same request; moving back below 11 clears it."""
    return await user_service.update_study_level(db, user, data)


@router.get("/me/stats", response_model=list[TopicStat])
async def my_stats(user: CurrentUser, db: DbSession):
    """Accuracy per subject/topic across submitted attempts, weakest first."""
    return await quiz_service.topic_stats(db, user)


@router.get("", response_model=list[UserOut])
async def list_users(user: StaffUser, db: DbSession, page: Pagination):
    return await user_service.list_org_users(db, user.org_id, page.limit, page.offset)


@router.patch("/{user_id}/role", response_model=UserOut)
async def update_role(user_id: uuid.UUID, data: RoleUpdate, admin: AdminUser, db: DbSession):
    return await user_service.set_role(db, admin, user_id, data.role)
