from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.security import create_access_token
from sag_api.db.models import User
from sag_api.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    SingleUserSessionResponse,
    SingleUserSetupRequest,
    TokenResponse,
    UserOut,
)
from sag_api.services.auth_service import (
    authenticate_or_register,
    get_single_user,
    initialize_single_user,
    register_user,
    reset_single_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _single_user_response(user: User | None) -> SingleUserSessionResponse:
    return SingleUserSessionResponse(
        setup_required=user is None,
        user=UserOut.model_validate(user) if user is not None else None,
    )


def _token_for(user: User) -> str:
    extra = (
        {"auth_version": user.auth_version}
        if settings.auth_mode == "password"
        else None
    )
    return create_access_token(user.id, extra=extra)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await register_user(
        session,
        email=body.email,
        password=body.password,
        name=body.name,
        bootstrap_token=body.bootstrap_token,
    )
    return TokenResponse(access_token=_token_for(user), user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    user = await authenticate_or_register(
        session,
        name=body.name,
        email=body.email,
        password=body.password,
        bootstrap_token=body.bootstrap_token,
    )
    return TokenResponse(access_token=_token_for(user), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/session", response_model=SingleUserSessionResponse)
async def single_user_session(
    session: AsyncSession = Depends(get_session),
) -> SingleUserSessionResponse:
    return _single_user_response(await get_single_user(session))


@router.post("/session", response_model=SingleUserSessionResponse, status_code=201)
async def initialize_single_user_session(
    body: SingleUserSetupRequest,
    session: AsyncSession = Depends(get_session),
) -> SingleUserSessionResponse:
    return _single_user_response(await initialize_single_user(session, name=body.name))


@router.delete("/session", status_code=204)
async def reset_single_user_session(
    session: AsyncSession = Depends(get_session),
) -> Response:
    if settings.auth_mode != "single_user":
        return Response(status_code=404)
    await reset_single_user(session)
    return Response(status_code=204)
