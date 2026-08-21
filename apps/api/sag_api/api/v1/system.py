from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import get_current_user, get_fnos_identity
from sag_api.core.errors import ApiError, ConflictError, NotFoundError
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import model_provider_catalog
from sag_api.db.models import FnOSMcpGrant, Source, User
from sag_api.fnos.identity import GatewayIdentity, derive_fnos_internal_key
from sag_api.generation import LLMClient
from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES
from sag_api.schemas.system import (
    FnOSMcpGrantCreate,
    ModelConfigUpdate,
    QuickModelSetupRequest,
    SystemPreferencesUpdate,
)
from sag_api.services import settings_service
from sag_api.tools.builtin import WebSearchTool

router = APIRouter(prefix="/system", tags=["system"])
log = get_logger("system")


def _capabilities() -> dict:
    return {
        "auth_mode": settings.auth_mode,
        "llm_configured": settings.llm_configured,
        "web_search_configured": WebSearchTool.configured(),
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "context_window": settings.llm_context_window,
        "embedding_model": settings.embedding_model,
        "document_parser": settings.document_parser,
        "effective_document_parser": settings.effective_document_parser,
        "mineru_configured": settings.mineru_configured,
        "vector_provider": settings.sag_vector_provider,
        "language": settings.sag_language,
        "search_strategy": settings.search_strategy,
        "timezone": settings.timezone,
        "max_upload_mb": settings.max_upload_mb,
        "allowed_upload_exts": sorted(settings.allowed_upload_exts),
    }


def _fnos_mcp_routing_key() -> bytes:
    if not settings.fnos_internal_secret_file:
        raise NotFoundError("资源不存在")
    return derive_fnos_internal_key(
        Path(settings.fnos_internal_secret_file), b"sag-fnos-mcp-routing-v1"
    )


def _fnos_mcp_grant_payload(grant: FnOSMcpGrant) -> dict:
    return {
        "id": grant.id,
        "expires_at": grant.expires_at,
        "revoked_at": grant.revoked_at,
        "created_at": grant.created_at,
    }


@router.get("/health")
async def health() -> dict:
    """存活探针：进程在跑即 200（不触碰依赖）。"""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """就绪探针：数据库可连通才 200，否则 503（供 compose/K8s 健康检查）。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("就绪检查失败：%s", e)
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": False})
    return JSONResponse(content={"status": "ready", "db": True})


@router.get("/capabilities")
async def capabilities() -> dict:
    """能力探测：供前端判断是否已配置 LLM、当前引擎后端等。"""
    return _capabilities()


@router.get("/model-config")
async def get_model_config(
    _user: User = Depends(get_current_user),
) -> dict:
    """当前生效的模型与检索配置（密钥脱敏为 *_set 布尔）。"""
    return settings_service.effective_model_config()


@router.get("/model-providers")
async def get_model_providers(
    _user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    """前后端共享的模型接入能力与技术默认值。"""
    return model_provider_catalog()


@router.get("/preferences")
async def get_system_preferences(
    _user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Presentation preferences shared by this local-first installation."""
    return settings_service.effective_system_preferences()


@router.put("/preferences")
async def update_system_preferences(
    body: SystemPreferencesUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    return await settings_service.save_system_preferences(
        session,
        body.model_dump(exclude_unset=True),
    )


@router.get("/model-setup")
async def get_model_setup_status(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """首次进入时判断是否需要展示快捷模型配置。"""
    return await settings_service.model_setup_status(session)


@router.get("/mcp")
async def knowledge_mcp_descriptor(
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """返回将整个 SAG 知识库挂入外部 MCP 宿主的连接信息。"""
    source_count = await session.scalar(select(func.count(Source.id))) or 0
    if settings.auth_mode == "fnos":
        grants = list(
            (
                await session.scalars(
                    select(FnOSMcpGrant)
                    .order_by(FnOSMcpGrant.created_at.desc(), FnOSMcpGrant.id.desc())
                )
            ).all()
        )
        now = datetime.now(UTC)
        active_grants = [
            grant for grant in grants if grant.revoked_at is None and grant.expires_at > now
        ]
        inactive_grants = [
            grant for grant in grants if grant.revoked_at is not None or grant.expires_at <= now
        ]
        return {
            "name": "SAG 知识库",
            "scope": "knowledge_base",
            "mode": "fnos",
            "source_count": source_count,
            "tools": list(MCP_TOOL_NAMES),
            "tool_details": list(MCP_TOOL_DETAILS),
            "grants": [_fnos_mcp_grant_payload(grant) for grant in active_grants],
            "inactive_grants": [_fnos_mcp_grant_payload(grant) for grant in inactive_grants],
            "http": {
                "transport": "streamable-http",
                "path": "/mcp/",
                "headers": {"Authorization": "Bearer <SAG_FNOS_MCP_TOKEN>"},
                "note": "请先生成 MCP 凭据；凭据只显示一次，过期或撤销后需要重新授权。",
            },
        }
    base = str(request.base_url).rstrip("/")
    return {
        "name": "SAG 知识库",
        "scope": "knowledge_base",
        "source_count": source_count,
        "tools": list(MCP_TOOL_NAMES),
        "tool_details": list(MCP_TOOL_DETAILS),
        "http": {
            "transport": "streamable-http",
            "url": f"{base}/mcp/",
            "headers": {"Authorization": "Bearer <SAG_TOKEN>"},
            "note": (
                "默认开放全部信源；Dify 等宿主请使用 streamable_http/Streamable HTTP 传输，"
                "可在 URL 添加 ?source_id=<id> 临时限定单个信源。"
            ),
        },
        "stdio": {
            "command": "python",
            "args": ["-m", "sag_api.mcp.server"],
            "env": {},
            "note": "默认开放全部信源；设置 SAG_MCP_SOURCE_ID 可限定单个信源。",
        },
    }


@router.post("/mcp/grants", status_code=201)
async def issue_fnos_mcp_grant(
    body: FnOSMcpGrantCreate,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(get_fnos_identity),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if settings.auth_mode != "fnos":
        raise NotFoundError("资源不存在")
    from sag_api.services.fnos_mcp_access import issue_grant

    issued = await issue_grant(
        session,
        user=_user,
        expires_in_days=body.expires_in_days,
        identity_uid=identity.uid,
        identity_username=identity.username,
        routing_key=_fnos_mcp_routing_key(),
    )
    return {
        "id": issued.id,
        "token": issued.token,
        "expires_in_days": body.expires_in_days,
        "expires_at": issued.expires_at,
    }


@router.delete("/mcp/grants/{grant_id}", status_code=204)
async def revoke_fnos_mcp_grant(
    grant_id: str,
    _user: User = Depends(get_current_user),
    _identity: GatewayIdentity = Depends(get_fnos_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if settings.auth_mode != "fnos":
        raise NotFoundError("资源不存在")
    from sag_api.services.fnos_mcp_access import revoke_grant

    if not await revoke_grant(session, grant_id=grant_id, user=_user):
        raise NotFoundError("MCP 凭据不存在")
    return Response(status_code=204)


@router.delete("/mcp/grants/{grant_id}/record", status_code=204)
async def delete_inactive_fnos_mcp_grant(
    grant_id: str,
    _user: User = Depends(get_current_user),
    _identity: GatewayIdentity = Depends(get_fnos_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if settings.auth_mode != "fnos":
        raise NotFoundError("资源不存在")
    from sag_api.services.fnos_mcp_access import delete_inactive_grant

    result = await delete_inactive_grant(session, grant_id=grant_id, user=_user)
    if result == "missing":
        raise NotFoundError("MCP 凭据不存在")
    if result == "active":
        raise ConflictError("请先撤销有效的 MCP 凭据")
    return Response(status_code=204)


@router.post("/model-setup/302")
async def quick_setup_302(
    body: QuickModelSetupRequest,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """只接收一个 302.AI Key，写入生成、向量、MinerU 与检索预设。"""
    status = await settings_service.model_setup_status(session)
    if not status["required"]:
        raise ConflictError("模型配置已存在，请在设置中修改")

    config = await settings_service.save_302_quick_setup(session, body.api_key)
    await request.app.state.engine_manager.aclose_all()
    return {"config": config, "capabilities": _capabilities()}


@router.put("/model-config")
async def update_model_config(
    body: ModelConfigUpdate,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """保存运行期配置；仅在模型/向量配置实际变化时安全重建引擎。"""
    patch = body.model_dump(exclude_unset=True)
    before = settings_service.effective_model_config()
    config = await settings_service.save_model_config(session, patch)

    # 解析器/检索参数保存无需打断暖引擎；只有引擎配置真的变化才安全重建。
    engine_fields = {
        "llm_provider",
        "llm_base_url",
        "llm_model",
        "llm_temperature",
        "llm_max_tokens",
        "llm_timeout_ms",
        "llm_max_retries",
        "embedding_model",
        "embedding_base_url",
        "embedding_dimensions",
        "sag_language",
    }
    engine_changed = any(before.get(key) != config.get(key) for key in engine_fields)
    engine_changed = engine_changed or bool(patch.get("llm_api_key") or patch.get("embedding_api_key"))
    if engine_changed:
        await request.app.state.engine_manager.aclose_all()
    return {"config": config, "capabilities": _capabilities()}


@router.post("/model-config/mineru/302")
async def configure_302_mineru(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """已有 302 LLM/Embedding 用户一键复用服务端保存的 Key 启用 MinerU。"""
    config = await settings_service.save_302_mineru_setup(session)
    return {"config": config, "capabilities": _capabilities()}


@router.post("/model-config/test")
async def test_model_config(
    request: Request,
    body: ModelConfigUpdate | None = None,
    _user: User = Depends(get_current_user),
) -> dict:
    """连接测试：优先验证表单草稿，不持久化也不修改运行期单例。"""
    llm: LLMClient
    active = settings
    if body is None:
        llm = request.app.state.llm
    else:
        patch = body.model_dump(exclude_unset=True)
        updates = {
            key: (None if key in {"llm_base_url"} and value == "" else value)
            for key, value in patch.items()
            if not (key == "llm_api_key" and not value)
        }
        active = settings.model_copy(update=updates)
        llm = LLMClient(active)
    if not llm.configured:
        return {"ok": False, "message": "尚未配置 API Key"}
    try:
        await llm.complete([{"role": "user", "content": "ping"}])
        return {
            "ok": True,
            "message": f"连接成功 · {active.llm_provider} / {active.llm_model}",
        }
    except ApiError as e:
        return {"ok": False, "message": e.message}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
