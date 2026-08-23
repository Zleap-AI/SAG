from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.security import create_access_token
from sag_api.db.models import User
from sag_api.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from sag_api.services.auth_service import authenticate_or_register, register_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await register_user(session, email=body.email, password=body.password, name=body.name)
    return TokenResponse(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    coordinator = getattr(request.app.state, "storage_bootstrap", None)  # [storage-bootstrap]
    maintenance_login = coordinator is not None and not coordinator.runtime_ready()  # [storage-bootstrap]
    user = await authenticate_or_register(
        session,
        name=body.name,
        email=body.email,
        password=body.password,
        allow_create=not maintenance_login,
        exact_existing=maintenance_login,
    )
    return TokenResponse(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
