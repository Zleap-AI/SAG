from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import ForbiddenError
from sag_api.core.security import create_access_token
from sag_api.db.models import User
from sag_api.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from sag_api.services.auth_service import authenticate, authenticate_or_register, register_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status")
async def auth_status(session: AsyncSession = Depends(get_session)) -> dict[str, bool | str]:
    if settings.auth_mode == "local":
        return {"mode": "local", "registration_required": False, "registration_open": False}

    credential_count = await session.scalar(
        select(func.count()).select_from(User).where(User.email != "")
    ) or 0
    registration_required = credential_count == 0
    return {
        "mode": "password",
        "registration_required": registration_required,
        "registration_open": registration_required or settings.allow_registration,
    }


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    if settings.auth_mode != "password":
        raise ForbiddenError("当前部署未启用邮箱密码认证")
    user = await register_user(session, email=body.email, password=body.password, name=body.name)
    return TokenResponse(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if settings.auth_mode == "password":
        user = await authenticate(session, email=body.email, password=body.password or "")
    else:
        coordinator = getattr(request.app.state, "storage_bootstrap", None)  # [storage-bootstrap]
        maintenance_login = coordinator is not None and not coordinator.runtime_ready()  # [storage-bootstrap]
        user = await authenticate_or_register(
            session,
            name=body.name,
            email="",
            password=None,
            allow_create=not maintenance_login,
            exact_existing=maintenance_login,
        )
    return TokenResponse(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
