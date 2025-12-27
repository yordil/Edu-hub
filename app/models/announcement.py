import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import Timestamped, UUIDPk
from app.models.user import User


class Announcement(UUIDPk, Timestamped, Base):
    __tablename__ = "announcements"

    # NULL = platform-wide announcement from EduHub, shown to every school.
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(default=False, server_default="false")

    author: Mapped[User | None] = relationship(lazy="joined")
