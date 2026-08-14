"""FastAPI 依赖：认证 + 应用级单例。单用户，无工作空间/角色。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from time import time

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.db import get_session
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import AuthError, ForbiddenError, NotFoundError
from sag_api.core.security import decode_token
from sag_api.db.models import User
from sag_api.fnos.identity import GatewayIdentity
from sag_api.generation import LLMClient
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.auth_service import get_single_user, get_user_for_token_payload

_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _fnos_identity_signer(secret_file: str):
    from sag_api.fnos.identity import InternalIdentitySigner

    return InternalIdentitySigner.from_file(Path(secret_file))


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    from sag_api.core.config import settings

    if settings.auth_mode == "fnos":
        from sag_api.services.fnos_user_service import get_or_create_fnos_user

        signer = _fnos_identity_signer(settings.fnos_internal_secret_file)
        identity = signer.verify(
            request.headers,
            expected_uid=settings.fnos_uid,
            now=int(time()),
            expected_username=settings.fnos_username if settings.fnos_username_isolation else None,
        )
        request.state.fnos_identity = identity
        user = await get_or_create_fnos_user(session, identity)
        request.state.user = user
        return user

    if settings.auth_mode == "single_user":
        user = await get_single_user(session)
        if user is None:
            raise AuthError("请先设置用户名")
        request.state.user = user
        return user
    if creds is None:
        raise AuthError("缺少认证令牌")
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError as e:
        raise AuthError("令牌无效或已过期") from e
    user = await get_user_for_token_payload(session, payload)
    if user is None:
        raise AuthError("令牌无效或已过期")
    request.state.user = user
    return user


def get_fnos_identity(request: Request) -> GatewayIdentity:
    """Return only the identity already verified by the private gateway hop."""
    from sag_api.core.config import settings

    if settings.auth_mode != "fnos":
        raise NotFoundError("资源不存在")
    identity = getattr(request.state, "fnos_identity", None)
    if not isinstance(identity, GatewayIdentity):
        raise AuthError("fnOS 身份未验证")
    return identity


def require_fnos_nas_admin(
    identity: GatewayIdentity = Depends(get_fnos_identity),
) -> GatewayIdentity:
    """Restrict NAS discovery and import mutations to a verified fnOS administrator."""
    if not identity.is_admin:
        raise ForbiddenError(
            "需要 fnOS 管理员权限",
            code=ErrorCode.NAS_ADMINISTRATOR_REQUIRED,
        )
    return identity


def get_engine_manager(request: Request) -> EngineManager:
    return request.app.state.engine_manager


def get_job_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue


def get_fnos_nas_access(request: Request):
    from sag_api.core.errors import NotFoundError

    service = getattr(request.app.state, "fnos_nas_access", None)
    if service is None:
        raise NotFoundError("资源不存在")
    return service


def get_fnos_nas_registry(request: Request):
    from sag_api.core.errors import NotFoundError

    registry = getattr(request.app.state, "fnos_nas_registry", None)
    if registry is None:
        raise NotFoundError("资源不存在")
    return registry


def get_fnos_nas_scanner(request: Request):
    from sag_api.core.errors import NotFoundError

    scanner = getattr(request.app.state, "fnos_nas_scanner", None)
    if scanner is None:
        raise NotFoundError("资源不存在")
    return scanner


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def get_tool_registry():
    """Agent 工具注册表（内置检索/实体工具 + 运行时注入的 MCP 工具）。"""
    from sag_api.tools import registry

    return registry
