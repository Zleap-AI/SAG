from __future__ import annotations

from ipaddress import ip_address

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import get_current_user, get_current_user_or_connector
from sag_api.core.errors import ApiError, ConflictError, ForbiddenError
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import model_provider_catalog
from sag_api.db.models import Source, User
from sag_api.generation import LLMClient
from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES
from sag_api.sag.engine_manager import EngineManager
from sag_api.schemas.dsh_integration import (
    DshCapabilityDescriptor,
    DshConnectionDescriptor,
    DshIntegrationUpdate,
    DshUploadCapability,
)
from sag_api.schemas.system import (
    ModelConfigUpdate,
    QuickModelSetupRequest,
    SystemPreferencesUpdate,
)
from sag_api.services import dsh_integration_service, settings_service

router = APIRouter(prefix="/system", tags=["system"])
log = get_logger("system")

DSH_CAPABILITIES = (
    "sources.list",
    "sources.create",
    "knowledge.search",
    "knowledge.read",
    "documents.list",
    "documents.get",
    "documents.upload",
    "documents.ingest",
    "documents.reprocess",
    "documents.delete",
)


def _request_is_loopback(request: Request) -> bool:
    """Return whether the ASGI peer is a loopback address."""
    if request.client is None:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _dsh_urls(request: Request) -> tuple[str, str]:
    """Build HTTP connection URLs from the address the caller actually used."""
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/api/v1", f"{base_url}/mcp/"


def _dsh_capability_descriptor(state: dsh_integration_service.DshIntegrationState) -> DshCapabilityDescriptor:
    """Project persisted source selection without exposing the connector credential."""
    return DshCapabilityDescriptor(
        capabilities=list(DSH_CAPABILITIES),
        upload=DshUploadCapability(
            maxMb=settings.max_upload_mb,
            extensions=sorted(extension.lstrip(".") for extension in settings.allowed_upload_exts),
        ),
        defaultSourceId=state.default_source_id,
    )


def _dsh_connection_descriptor(
    request: Request,
    state: dsh_integration_service.DshIntegrationState,
) -> DshConnectionDescriptor:
    """Build a directly usable descriptor for the current HTTP origin."""
    api_url, mcp_url = _dsh_urls(request)
    return DshConnectionDescriptor(
        name="SAG 知识库",
        apiUrl=api_url,
        mcpUrl=mcp_url,
        accessToken=state.token,
        defaultSourceId=state.default_source_id,
    )


def _capabilities() -> dict:
    strategy_report = EngineManager.strategies_capability_report(settings)
    return {
        "llm_configured": settings.llm_configured,
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
        "search_strategies": strategy_report["enabled"],
        "search_strategies_disabled": strategy_report["disabled"],
        "timezone": settings.timezone,
        "max_upload_mb": settings.max_upload_mb,
        "allowed_upload_exts": sorted(settings.allowed_upload_exts),
    }


@router.get("/health")
async def health() -> dict:
    """存活探针：进程在跑即 200（不触碰依赖）。"""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """就绪探针：数据库与知识运行时均就绪才返回 200。"""
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        log.warning("就绪检查失败：%s", e)
        return JSONResponse(status_code=503, content={"status": "unavailable", "db": False})

    runtime = getattr(request.app.state, "knowledge_runtime", None)
    coordinator = getattr(request.app.state, "storage_bootstrap", None)  # [storage-bootstrap]
    if runtime is None or not runtime.ready:
        content: dict[str, object] = {"status": "unavailable", "db": True}
        if coordinator is not None:
            content["phase"] = coordinator.public_status()["phase"]
        return JSONResponse(status_code=503, content=content)
    return JSONResponse(content={"status": "ready", "db": True})


@router.get("/capabilities")
async def capabilities() -> dict:
    """能力探测：供前端判断是否已配置 LLM、当前引擎后端等。"""
    return _capabilities()


@router.get("/dsh", response_model=DshCapabilityDescriptor)
async def dsh_capabilities(
    _user: User = Depends(get_current_user_or_connector),
    session: AsyncSession = Depends(get_session),
) -> DshCapabilityDescriptor:
    """Return connector capabilities without exposing its credential."""
    state = await dsh_integration_service.get_or_create_state(session)
    return _dsh_capability_descriptor(state)


@router.get("/dsh-connection", response_model=DshConnectionDescriptor)
async def discover_dsh_connection(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> DshConnectionDescriptor:
    """Return the local connector credential only to a loopback peer."""
    if not _request_is_loopback(request):
        raise ForbiddenError("仅允许本机访问 DSH 连接配置")
    state = await dsh_integration_service.get_or_create_state(session)
    return _dsh_connection_descriptor(request, state)


@router.get("/dsh/export")
async def export_dsh_connection(
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download the connector descriptor after normal SAG authentication."""
    state = await dsh_integration_service.get_or_create_state(session)
    descriptor = _dsh_connection_descriptor(request, state)
    return Response(
        content=descriptor.model_dump_json(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sag-dsh.json"'},
    )


@router.put("/dsh/settings", response_model=DshCapabilityDescriptor)
async def update_dsh_settings(
    body: DshIntegrationUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DshCapabilityDescriptor:
    """Select the source used by DSH when a request omits one."""
    state = await dsh_integration_service.update_default_source(session, body.default_source_id)
    await dsh_integration_service.write_connection_file(session)
    return _dsh_capability_descriptor(state)


@router.post("/dsh/regenerate", response_model=DshCapabilityDescriptor)
async def regenerate_dsh_connection_token(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DshCapabilityDescriptor:
    """Rotate the connector credential without returning the new secret."""
    state = await dsh_integration_service.regenerate_token(session)
    await dsh_integration_service.write_connection_file(session)
    return _dsh_capability_descriptor(state)


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
    await request.app.state.knowledge_runtime.apply_settings(settings, reset_engines=True)
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
    await request.app.state.knowledge_runtime.apply_settings(
        settings,
        reset_engines=engine_changed,
    )
    return {"config": config, "capabilities": _capabilities()}


@router.post("/model-config/mineru/302")
async def configure_302_mineru(
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """已有 302 LLM/Embedding 用户一键复用服务端保存的 Key 启用 MinerU。"""
    config = await settings_service.save_302_mineru_setup(session)
    await request.app.state.knowledge_runtime.apply_settings(settings, reset_engines=False)
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
