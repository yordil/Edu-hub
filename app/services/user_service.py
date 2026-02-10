import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.curriculum import STREAM_FROM_GRADE, validate_study_level
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.security import DUMMY_HASH, generate_join_code, hash_password, verify_password
from app.models.organization import Organization
from app.models.user import Role, User
from app.schemas.user import OrganizationRegister, ProfileUpdate, StudentRegister


async def _commit_new_user(db: AsyncSession, user: User) -> User:
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("Email already registered") from None
    return user


async def register_organization(db: AsyncSession, data: OrganizationRegister) -> User:
    org = Organization(
        name=data.organization_name.strip(), join_code=generate_join_code(), teacher_code=generate_join_code()
    )
    user = User(
        organization=org,
        email=data.email,
        full_name=data.full_name.strip(),
        hashed_password=hash_password(data.password),
        role=Role.admin,
    )
    return await _commit_new_user(db, user)


async def register_student(db: AsyncSession, data: StudentRegister) -> User:
    """Join with a school code: the student code makes a student, the teacher code a teacher."""
    code = data.join_code.strip().upper()
    org = await db.scalar(
        select(Organization).where(
            or_(Organization.join_code == code, Organization.teacher_code == code),
            Organization.is_internal.is_(False),
        )
    )
    if org is None:
        raise ValidationFailed("Invalid join code")
    is_teacher = org.teacher_code == code
    user = User(
        organization=org,
        email=data.email,
        full_name=data.full_name.strip(),
        hashed_password=hash_password(data.password),
        role=Role.teacher if is_teacher else Role.student,
        grade=None if is_teacher else data.grade,
        stream=None if is_teacher else data.stream,
    )
    return await _commit_new_user(db, user)


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, user.hashed_password) or not user.is_active:
        return None
    return user


async def update_study_level(db: AsyncSession, user: User, data: ProfileUpdate) -> User:
    """Apply a grade and/or stream change, checked as a pair.

    Grade can't simply advance on its own each September: the step from 10 to 11 is exactly when
    a student chooses a stream, and only they can make that choice. So it is always explicit.
    """
    if user.role is not Role.student:
        raise ValidationFailed("Only students set a grade or stream")
    sent = data.model_fields_set
    grade = data.grade if "grade" in sent else user.grade
    if "stream" in sent:
        stream = data.stream
    else:
        # Moving back below grade 11 ends the stream; otherwise the current one carries over.
        stream = None if grade is not None and grade < STREAM_FROM_GRADE else user.stream
    try:
        validate_study_level(grade, stream)
    except ValueError as e:
        raise ValidationFailed(str(e)) from None
    user.grade, user.stream = grade, stream
    await db.commit()
    return user


async def list_org_users(db: AsyncSession, org_id: uuid.UUID, limit: int, offset: int) -> list[User]:
    stmt = select(User).where(User.org_id == org_id).order_by(User.created_at, User.id).limit(limit).offset(offset)
    return list(await db.scalars(stmt))


async def set_role(db: AsyncSession, actor: User, user_id: uuid.UUID, role: Role) -> User:
    if user_id == actor.id:
        raise ValidationFailed("You cannot change your own role")
    target = await db.get(User, user_id)
    if target is None or target.org_id != actor.org_id:
        raise NotFoundError("User not found")
    target.role = role
    await db.commit()
    return target


async def rotate_join_code(db: AsyncSession, actor: User, kind: str = "student") -> Organization:
    org = await db.get(Organization, actor.org_id)
    if kind == "teacher":
        org.teacher_code = generate_join_code()
    else:
        org.join_code = generate_join_code()
    await db.commit()
    return org


INTERNAL_ORG_NAME = "EduHub HQ"


async def create_platform_admin(db: AsyncSession, email: str, password: str, full_name: str) -> User:
    """Create (or promote) an EduHub operator inside the internal HQ workspace."""
    email = email.strip().lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing:
        existing.is_platform_admin = True
        await db.commit()
        return existing
    hq = await db.scalar(select(Organization).where(Organization.is_internal.is_(True)))
    if hq is None:
        hq = Organization(
            name=INTERNAL_ORG_NAME, join_code=generate_join_code(), teacher_code=generate_join_code(), is_internal=True
        )
    user = User(
        organization=hq,
        email=email,
        full_name=full_name,
        hashed_password=hash_password(password),
        role=Role.admin,
        is_platform_admin=True,
    )
    return await _commit_new_user(db, user)
