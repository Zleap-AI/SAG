from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.connectors import registry
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager, get_job_queue
from sag_api.db.models import User
from sag_api.jobs import JobQueue
from sag_api.mcp.server import MCP_TOOL_DETAILS, MCP_TOOL_NAMES
from sag_api.sag import EngineManager
from sag_api.schemas.common import Ok
from sag_api.schemas.job import JobOut
from sag_api.schemas.source import ConnectorOut, SourceCreate, SourceOut, SourceUpdate
from sag_api.services.source_service import (
    create_source,
    delete_source,
    get_source,
    list_sources,
    sync_source,
    update_source,
)

router = APIRouter(prefix="/sources", tags=["sources"])


# 注意：静态路由须在 /{source_id} 之前声明
@router.get("/connectors", response_model=list[ConnectorOut])
async def list_connectors() -> list[ConnectorOut]:
    return [ConnectorOut(**c.meta.to_public()) for c in registry.all()]


@router.get("", response_model=list[SourceOut])
async def list_(
    _user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[SourceOut]:
    return [SourceOut.model_validate(s) for s in await list_sources(session)]


@router.post("", response_model=SourceOut, status_code=201)
async def create(
    body: SourceCreate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> SourceOut:
    source = await create_source(session, body, engine_manager=engine_manager)
    return SourceOut.model_validate(source)


@router.get("/{source_id}", response_model=SourceOut)
async def get_(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    return SourceOut.model_validate(await get_source(session, source_id))


@router.patch("/{source_id}", response_model=SourceOut)
async def update_(
    source_id: str,
    body: SourceUpdate,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> SourceOut:
    return SourceOut.model_validate(
        await update_source(session, source_id, body, job_queue=job_queue)
    )


@router.delete("/{source_id}", response_model=Ok)
async def delete_(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    job_queue: JobQueue = Depends(get_job_queue),
) -> Ok:
    from sag_api.core.config import settings

    await delete_source(
        session,
        source_id,
        engine_manager=engine_manager,
        upload_dir=settings.upload_dir,
        job_queue=job_queue,
    )
    return Ok(detail="信源已删除")


@router.get("/{source_id}/chunks/{chunk_id}")
async def get_chunk(
    source_id: str,
    chunk_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> dict:
    """引用溯源：读取某分块的完整原文。"""
    from sag_api.core.errors import NotFoundError

    source = await get_source(session, source_id)
    chunk = await engine_manager.get_chunk(source.sag_source_config_id, chunk_id, source=source)
    if chunk is None:
        raise NotFoundError("原文分块不存在")
    return {**chunk.model_dump(), "source_id": source.id, "source_name": source.name}


@router.get("/{source_id}/mcp")
async def mcp_descriptor(
    source_id: str,
    request: Request,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """信源即 MCP：返回把该信源挂进外部宿主（Claude Desktop / Cursor）的连接信息。"""
    source = await get_source(session, source_id)
    base = str(request.base_url).rstrip("/")
    return {
        "source_id": source.id,
        "source_name": source.name,
        "tools": list(MCP_TOOL_NAMES),
        "tool_details": list(MCP_TOOL_DETAILS),
        "http": {
            "transport": "streamable-http",
            "url": f"{base}/mcp/?source_id={source.id}",
            "headers": {"Authorization": "Bearer <SAG_TOKEN>"},
            "note": (
                "在支持 Streamable HTTP MCP 的宿主中填此 URL；"
                "Dify 配置可使用 transport=streamable_http，并在 Authorization 头携带 Bearer <token>。"
            ),
        },
        "stdio": {
            "command": "python",
            "args": ["-m", "sag_api.mcp.server"],
            "env": {"SAG_MCP_SOURCE_ID": source.id},
            "note": "面向仅支持 stdio 的宿主；需在 apps/api 的 Python 环境下运行。",
        },
    }


@router.post("/{source_id}/sync", response_model=JobOut)
async def sync(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> JobOut:
    job = await sync_source(session, source_id, job_queue=job_queue)
    return JobOut.model_validate(job)
