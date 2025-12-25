import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.core.curriculum import Stream, validate_study_level
from app.models.user import Role

Grade = Annotated[int, Field(ge=9, le=12)]


def _bcrypt_byte_limit(v: str) -> str:
    if len(v.encode()) > 72:
        raise ValueError("password must be at most 72 bytes")
    return v


Password = Annotated[str, Field(min_length=8, max_length=72), AfterValidator(_bcrypt_byte_limit)]
Email = Annotated[EmailStr, AfterValidator(str.lower)]
FullName = Annotated[str, Field(min_length=1, max_length=120)]


class OrganizationRegister(BaseModel):
    organization_name: str = Field(min_length=2, max_length=120)
    full_name: FullName
    email: Email
    password: Password


class StudentRegister(BaseModel):
    join_code: str = Field(min_length=4, max_length=16)
    full_name: FullName
    email: Email
    password: Password
    # Required for students in the UI; both are ignored when joining with a teacher code.
    grade: Grade | None = None
    stream: Stream | None = None

    @model_validator(mode="after")
    def _study_level(self):
        validate_study_level(self.grade, self.stream)
        return self


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    grade: int | None
    stream: Stream | None
    is_active: bool
    is_platform_admin: bool
    created_at: datetime


class ProfileUpdate(BaseModel):
    """Change grade, stream, or both. Whatever is omitted keeps its current value, and the result
    is checked as a pair — so moving from grade 10 to 11 needs a stream in the same request."""

    grade: Grade | None = None
    stream: Stream | None = None

    @model_validator(mode="after")
    def _something(self):
        if not self.model_fields_set:
            raise ValueError("send a grade, a stream, or both")
        return self


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    join_code: str
    created_at: datetime


class OrganizationAdminOut(OrganizationOut):
    teacher_code: str


class RoleUpdate(BaseModel):
    role: Role


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    email: Email


class PasswordResetIssued(BaseModel):
    """Returned only to the web app, over the authenticated internal call. `token` is null when there is
    no active account for the email; the web app shows the same message to the person either way."""

    token: str | None = None
    email: str | None = None
    full_name: str | None = None


class PasswordResetCheck(BaseModel):
    token: str = Field(min_length=10, max_length=2000)


class PasswordResetConfirm(PasswordResetCheck):
    new_password: Password
