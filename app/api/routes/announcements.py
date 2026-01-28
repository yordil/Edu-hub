import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, Pagination, StaffUser
from app.schemas.platform import AnnouncementCreate, AnnouncementOut
from app.services import announcement_service

router = APIRouter(prefix="/announcements", tags=["announcements"])


@router.get("", response_model=list[AnnouncementOut])
async def list_announcements(user: CurrentUser, db: DbSession, page: Pagination):
    """Your school's announcements plus EduHub-wide ones; pinned first, then newest."""
    return [announcement_service.to_out(a) for a in await announcement_service.list_for(db, user, page.limit, page.offset)]


@router.post("", response_model=AnnouncementOut, status_code=status.HTTP_201_CREATED)
async def create_announcement(data: AnnouncementCreate, user: StaffUser, db: DbSession):
    """Visible only to members of the author's school."""
    return announcement_service.to_out(await announcement_service.create(db, user, data))


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(announcement_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Admins can delete any school announcement; teachers only their own."""
    await announcement_service.delete(db, user, announcement_id)
