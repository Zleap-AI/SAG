from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager
from sag_api.core.errors import NotFoundError, ValidationError
from sag_api.db.models import User
from sag_api.sag import EngineManager
from sag_api.schemas.chunk import (
    EntityContextOut,
    GrepMatchOut,
    GrepResponse,
    OutlineOut,
    ReadResponse,
)
from sag_api.services.document_service import get_public_document, read_document_lines
from sag_api.services.outline_service import build_outline, outline_rows
from sag_api.services.source_service import get_source

router = APIRouter(prefix="/sources/{source_id}", tags=["knowledge"])


@router.get("/outline", response_model=OutlineOut)
async def outline(
    source_id: str,
    document_id: str = Query(min_length=1),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> OutlineOut:
    """文档大纲：标题 + chunk_id，按阅读顺序排列，并附带标题层级与章节路径。"""
    source = await get_source(session, source_id)
    document = await get_public_document(session, source, document_id)
    if not document.sag_source_id:
        raise NotFoundError("文档尚无大纲，可能仍在处理中")
    rows = await engine_manager.list_chunk_headings(
        source.sag_source_config_id,
        source=source,
        doc_sag_id=document.sag_source_id,
    )
    if not rows:
        raise NotFoundError("文档尚无大纲，可能仍在处理中")
    markdown = await engine_manager.get_document_markdown(
        source.sag_source_config_id,
        document.sag_source_id,
        source=source,
    )
    return OutlineOut(
        document_id=document.id,
        filename=document.filename,
        outline=outline_rows(build_outline(markdown, rows)),
    )


@router.get("/grep", response_model=GrepResponse)
async def grep(
    source_id: str,
    pattern: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> GrepResponse:
    """精确文本匹配：按原文字面内容查找，大小写不敏感。"""
    source = await get_source(session, source_id)
    rows = await engine_manager.grep_chunks(
        source.sag_source_config_id,
        pattern,
        source=source,
        limit=limit,
    )
    matches = [
        GrepMatchOut(
            chunk_id=row["chunk_id"],
            heading=row["heading"],
            snippet=row["snippet"],
        )
        for row in rows
    ]
    return GrepResponse(pattern=pattern, matches=matches, count=len(matches))


@router.get("/documents/{document_id}/read", response_model=ReadResponse)
async def read(
    source_id: str,
    document_id: str,
    offset: int = Query(default=1, ge=1),
    limit: int = Query(default=120, ge=1, le=500),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> ReadResponse:
    """按行分页读取文档文本：文本类读原文件，PDF / Office 等读解析后的 Markdown。"""
    source = await get_source(session, source_id)
    document = await get_public_document(session, source, document_id)
    try:
        all_lines = await read_document_lines(document, source, engine_manager)
    except OSError as exc:
        raise NotFoundError("文件读取失败") from exc
    if all_lines is None:
        raise NotFoundError("文档尚无可读文本，可能仍在处理中或原始文件已清理")
    total = len(all_lines)
    start = max(0, offset - 1)
    page = all_lines[start : start + limit]
    if not page:
        raise ValidationError(f"超出范围：全文共 {total} 行")
    return ReadResponse(
        document_id=document.id,
        filename=document.filename,
        total_lines=total,
        offset=start + 1,
        limit=len(page),
        lines=page,
    )


@router.get("/entities/{name}/context", response_model=EntityContextOut)
async def entity_context(
    source_id: str,
    name: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> EntityContextOut:
    """查询实体的相关事件上下文。先精确名称匹配、再子串匹配。"""
    source = await get_source(session, source_id)
    target = name.strip()
    if not target:
        raise ValidationError("实体名称不能为空")
    entities = await engine_manager.list_entities(
        source.sag_source_config_id, source=source, limit=200
    )
    lowered = target.lower()
    match = next(
        (entity for entity in entities if (entity.name or "").lower() == lowered),
        None,
    )
    if match is None:
        match = next(
            (entity for entity in entities if lowered in (entity.name or "").lower()),
            None,
        )
    if match is None:
        raise NotFoundError(f"未找到实体「{target}」")
    snippets = await engine_manager.entity_context(
        source.sag_source_config_id, match.id, source=source, limit=6
    )
    context = "\n\n".join(snippets) if snippets else (match.description or "")
    return EntityContextOut(
        entity_id=match.id,
        name=match.name,
        type=match.type,
        description=match.description,
        context=context,
        source_id=source.id,
        source_name=source.name,
    )
