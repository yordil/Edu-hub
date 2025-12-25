import enum
import uuid

from sqlalchemy import CheckConstraint, Enum, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.curriculum import Stream
from app.core.db import Base
from app.models.base import Timestamped, UUIDPk
from app.models.organization import Organization


class Role(str, enum.Enum):
    student = "student"
    teacher = "teacher"
    admin = "admin"


class User(UUIDPk, Timestamped, Base):
    __tablename__ = "users"
    # Mirrors curriculum.validate_study_level, so no code path can store an impossible pair.
    __table_args__ = (
        CheckConstraint("grade IS NULL OR grade BETWEEN 9 AND 12", name="grade_range"),
        CheckConstraint("grade IS NULL OR grade >= 11 OR stream IS NULL", name="no_stream_before_11"),
        CheckConstraint("grade IS NULL OR grade <= 10 OR stream IS NOT NULL", name="stream_from_11"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    is_active: Mapped[bool] = mapped_column(default=True)
    # 9-12. Decides the *level* of content a student draws: nothing tagged above it. Null for staff,
    # and for students who signed up before grades existed.
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    # Natural or social science track, from grade 11. Decides *which subjects*. Null for staff.
    stream: Mapped[Stream | None] = mapped_column(Enum(Stream, native_enum=False, length=16))
    # EduHub operator: sees cross-school metrics and manages the shared library.
    is_platform_admin: Mapped[bool] = mapped_column(default=False, server_default="false")

    organization: Mapped[Organization] = relationship(lazy="joined")
