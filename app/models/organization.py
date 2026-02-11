from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import Timestamped, UUIDPk


class Organization(UUIDPk, Timestamped, Base):
    """A tenant (school, bootcamp, company). All learning data is scoped to one."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(120))
    join_code: Mapped[str] = mapped_column(String(16), unique=True)
    # Signing up with this code makes the user a teacher instead of a student.
    teacher_code: Mapped[str] = mapped_column(String(16), unique=True)
    # The EduHub staff workspace; excluded from platform metrics.
    is_internal: Mapped[bool] = mapped_column(default=False, server_default="false")
