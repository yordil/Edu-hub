import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.user import (
    OrganizationRegister,
    PasswordResetCheck,
    PasswordResetConfirm,
    PasswordResetIssued,
    PasswordResetRequest,
    StudentRegister,
    Token,
)
from app.services import password_service, user_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_for(user: User) -> Token:
    return Token(access_token=create_access_token(user.id, user.org_id, user.role.value))


@router.post("/organizations", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register_organization(data: OrganizationRegister, db: DbSession) -> Token:
    """Create a new organization; the caller becomes its admin."""
    return _token_for(await user_service.register_organization(db, data))


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register_student(data: StudentRegister, db: DbSession) -> Token:
    """Join an existing organization as a student using its join code."""
    return _token_for(await user_service.register_student(db, data))


@router.post("/login", response_model=Token)
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession) -> Token:
    user = await user_service.authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_for(user)


def _internal_caller(x_internal_key: Annotated[str | None, Header()] = None) -> None:
    """Only the web app may ask for a reset token. Anyone else gets a 404, as if the route weren't there."""
    key = get_settings().internal_api_key
    if not key or not x_internal_key or not hmac.compare_digest(key.encode(), x_internal_key.encode()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.post(
    "/password-reset/request",
    response_model=PasswordResetIssued,
    dependencies=[Depends(_internal_caller)],
    include_in_schema=False,
)
async def request_password_reset(
    data: PasswordResetRequest, db: DbSession, x_client_ip: Annotated[str | None, Header()] = None
) -> PasswordResetIssued:
    """Internal: a reset token for the web app to email. The person's IP comes from the web app,
    since this call itself arrives from the web server."""
    issued = await password_service.issue_reset(db, data.email, x_client_ip)
    if issued is None:
        return PasswordResetIssued()
    return PasswordResetIssued(token=issued.token, email=issued.user.email, full_name=issued.user.full_name)


@router.post("/password-reset/check", status_code=status.HTTP_204_NO_CONTENT)
async def check_password_reset(data: PasswordResetCheck, db: DbSession) -> None:
    """204 if the link can still be used, 400 if it is invalid, expired or already used."""
    await password_service.check_reset(db, data.token)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(data: PasswordResetConfirm, db: DbSession) -> None:
    """Set a new password. The link stops working once it has been used."""
    await password_service.confirm_reset(db, data.token, data.new_password)
