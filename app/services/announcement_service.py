import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.announcement import Announcement
from app.models.user import Role, User
from app.schemas.platform import AnnouncementCreate, AnnouncementOut


def to_out(a: Announcement) -> AnnouncementOut:
    return AnnouncementOut(
        id=a.id,
        scope="platform" if a.org_id is None else "school",
        title=a.title,
        body=a.body,
        pinned=a.pinned,
        author_id=a.author_id,
        author_name=a.author.full_name if a.author else None,
        created_at=a.created_at,
    )


async def list_for(db: AsyncSession, user: User, limit: int, offset: int) -> list[Announcement]:
    """The user's school announcements plus EduHub-wide ones; pinned first, then newest."""
    stmt = (
        select(Announcement)
        .where(or_(Announcement.org_id == user.org_id, Announcement.org_id.is_(None)))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc(), Announcement.id)
        .limit(limit)
        .offset(offset)
    )
    return list(await db.scalars(stmt))


async def create(db: AsyncSession, author: User, data: AnnouncementCreate, platform_wide: bool = False) -> Announcement:
    announcement = Announcement(
        org_id=None if platform_wide else author.org_id,
        author=author,
        title=data.title.strip(),
        body=data.body.strip(),
        pinned=data.pinned,
    )
    db.add(announcement)
    await db.commit()
    return announcement


async def delete(db: AsyncSession, user: User, announcement_id: uuid.UUID) -> None:
    a = await db.get(Announcement, announcement_id)
    if a is None or (a.org_id is not None and a.org_id != user.org_id):
        raise NotFoundError("Announcement not found")
    if a.org_id is None:
        allowed = user.is_platform_admin
    else:
        allowed = user.role is Role.admin or (user.role is Role.teacher and a.author_id == user.id)
    if not allowed:
        raise ForbiddenError("You can't delete this announcement")
    await db.delete(a)
    await db.commit()
