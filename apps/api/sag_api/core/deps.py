"""FastAPI 依赖：认证 + 应用级单例。单用户，无工作空间/角色。"""

from __future__ import annotations

from typing import Literal

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.errors import AuthError
from sag_api.core.security import decode_token
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.auth_service import get_user
from sag_api.services.dsh_integration_service import authenticate_connector

_bearer = HTTPBearer(auto_error=False)
_AuthKind = Literal["jwt", "connector"]


def _require_matching_auth_mode(payload: dict) -> None:
    if settings.auth_mode == "password" and payload.get("auth_mode") != "password":
        raise AuthError("认证模式已变更，请重新登录")


async def _get_bearer_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if creds is None:
        raise AuthError("缺少认证令牌")
    return creds.credentials


async def _authenticate_user_principal(
    session: AsyncSession,
    token: str,
) -> tuple[User, _AuthKind] | None:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        user = await authenticate_connector(session, token)
        return (user, "connector") if user is not None else None
    user_id = payload.get("sub")
    _require_matching_auth_mode(payload)
    user = await get_user(session, user_id) if user_id else None
    return (user, "jwt") if user is not None and user.is_active else None


async def authenticate_user_token(session: AsyncSession, token: str) -> User | None:
    """Authenticate either token kind for user-only callers such as MCP."""
    principal = await _authenticate_user_principal(session, token)
    return principal[0] if principal is not None else None


async def get_current_user(
    request: Request,
    token: str = Depends(_get_bearer_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as error:
        raise AuthError("令牌无效或已过期") from error
    user_id = payload.get("sub")
    _require_matching_auth_mode(payload)
    user = await get_user(session, user_id) if user_id else None
    if user is None or not user.is_active:
        raise AuthError("用户不存在或已停用")
    request.state.user = user
    request.state.auth_kind = "jwt"
    return user


async def get_current_user_or_connector(
    request: Request,
    token: str = Depends(_get_bearer_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Authenticate approved knowledge operations with JWT or local connector token."""
    principal = await _authenticate_user_principal(session, token)
    if principal is None:
        raise AuthError("令牌无效或已过期")
    user, auth_kind = principal
    request.state.user = user
    request.state.auth_kind = auth_kind
    return user


def get_engine_manager(request: Request) -> EngineManager:
    return request.app.state.engine_manager


def get_job_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def get_tool_registry():
    """Agent 工具注册表（内置检索/实体工具 + 运行时注入的 MCP 工具）。"""
    from sag_api.tools import registry

    return registry
