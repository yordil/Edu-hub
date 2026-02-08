from typing import Literal

from fastapi import APIRouter

from app.api.deps import AdminUser, DbSession, StaffUser
from app.models.user import Role
from app.schemas.user import OrganizationAdminOut, OrganizationOut
from app.services import user_service

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("/me", response_model=OrganizationAdminOut | OrganizationOut)
async def my_organization(user: StaffUser):
    """Admins also receive the teacher invite code."""
    if user.role is Role.admin:
        return OrganizationAdminOut.model_validate(user.organization)
    return OrganizationOut.model_validate(user.organization)


@router.post("/me/join-code", response_model=OrganizationAdminOut)
async def rotate_join_code(admin: AdminUser, db: DbSession, kind: Literal["student", "teacher"] = "student"):
    """Issue a new student (default) or teacher code; the old one stops working immediately."""
    return await user_service.rotate_join_code(db, admin, kind)
