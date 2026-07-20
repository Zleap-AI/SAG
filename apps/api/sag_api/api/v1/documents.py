from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager, get_job_queue
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.models import User
from sag_api.enums import DocumentStatus
from sag_api.jobs import JobQueue
from sag_api.parsing.text import (
    TextDecodingError,
    is_text_preview,
    read_text_file,
)
from sag_api.sag import EngineManager
from sag_api.schemas.common import Ok
from sag_api.schemas.document import DocumentOut, IngestRequest
from sag_api.schemas.job import JobOut
from sag_api.services.document_service import (
    create_document_from_upload,
    delete_document,
    get_document,
    ingest_content,
    list_documents,
    pause_document,
    reprocess_document,
    resume_document,
)
from sag_api.services.source_service import get_source

router = APIRouter(prefix="/sources/{source_id}/documents", tags=["documents"])


def _check_extension(filename: str | None) -> None:
    """按白名单校验上传扩展名（空白名单 = 不限制）。"""
    allowed = settings.allowed_upload_exts
    if not allowed:
        return
    name = (filename or "").lower()
    if "." not in name or ("." + name.rsplit(".", 1)[1]) not in allowed:
        pretty = "、".join(sorted(e.lstrip(".") for e in allowed))
        raise ValidationError(f"不支持的文件类型。可上传：{pretty}")


@router.get("", response_model=list[DocumentOut])
async def list_(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentOut]:
    source = await get_source(session, source_id)
    return [DocumentOut.model_validate(d) for d in await list_documents(session, source.id)]


@router.post("", response_model=DocumentOut, status_code=201)
async def upload(
    source_id: str,
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> DocumentOut:
    source = await get_source(session, source_id)
    _check_extension(file.filename)
    data = await file.read()
    if not data:
        raise ValidationError("文件内容为空")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValidationError(f"文件超过 {settings.max_upload_mb}MB 上限")
    document, _job = await create_document_from_upload(
        session,
        source,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        upload_dir=settings.upload_dir,
        job_queue=job_queue,
    )
    return DocumentOut.model_validate(document)


@router.post("/ingest", response_model=DocumentOut, status_code=201)
async def ingest(
    source_id: str,
    body: IngestRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> DocumentOut:
    """统一写入接口：外部系统持续推送文本 / 消息进入信源。"""
    source = await get_source(session, source_id)
    document = await ingest_content(
        session,
        source,
        text=body.text,
        title=body.title,
        messages=[m.model_dump() for m in body.messages] if body.messages else None,
        upload_dir=settings.upload_dir,
        job_queue=job_queue,
    )
    return DocumentOut.model_validate(document)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    source = await get_source(session, source_id)
    return DocumentOut.model_validate(await get_document(session, source, document_id))


@router.get("/{document_id}/file")
async def get_file(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """原始文件（预览/下载）。文件已被清理时返回 404。"""
    import os

    from fastapi.responses import FileResponse

    from sag_api.core.errors import NotFoundError

    source = await get_source(session, source_id)
    document = await get_document(session, source, document_id)
    if not document.storage_path or not os.path.isfile(document.storage_path):
        raise NotFoundError("原始文件不存在或已被清理")
    return FileResponse(
        document.storage_path,
        media_type=document.content_type or "application/octet-stream",
        filename=document.filename,
        content_disposition_type="inline",
    )


@router.get("/{document_id}/preview")
async def get_preview(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """返回浏览器可直接消费的预览；文本统一转为 UTF-8，下载仍保留原字节。"""
    import os

    from fastapi.responses import FileResponse, Response

    source = await get_source(session, source_id)
    document = await get_document(session, source, document_id)
    if not document.storage_path or not os.path.isfile(document.storage_path):
        raise NotFoundError("原始文件不存在或已被清理")
    if is_text_preview(document.filename, document.content_type):
        try:
            decoded = await asyncio.to_thread(read_text_file, document.storage_path)
        except TextDecodingError as error:
            raise ValidationError(f"文本预览编码识别失败：{error}") from error
        return Response(
            content=decoded.text,
            media_type="text/plain; charset=utf-8",
            headers={"X-Muse-Source-Encoding": decoded.encoding},
        )
    return FileResponse(
        document.storage_path,
        media_type=document.content_type or "application/octet-stream",
        filename=document.filename,
        content_disposition_type="inline",
    )


@router.get("/{document_id}/parsed")
async def get_parsed(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
):
    """返回文档成功入库时保存的整篇 Markdown，不在读取时触发重新解析。"""
    from fastapi.responses import Response

    source = await get_source(session, source_id)
    document = await get_document(session, source, document_id)
    if document.status != DocumentStatus.READY:
        if document.status == DocumentStatus.FAILED:
            raise ConflictError(document.error or "文档解析失败，暂无解析内容")
        raise ConflictError("文档尚未解析完成")
    if not document.sag_source_id:
        raise NotFoundError("解析内容不存在，请重新处理文档")

    markdown = await engine_manager.get_document_markdown(
        source.sag_source_config_id,
        document.sag_source_id,
        source=source,
    )
    if not markdown:
        raise NotFoundError("解析内容不存在，请重新处理文档")
    return Response(content=markdown, media_type="text/markdown")


@router.post("/{document_id}/reprocess", response_model=JobOut)
async def reprocess(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> JobOut:
    source = await get_source(session, source_id)
    job = await reprocess_document(
        session,
        source,
        document_id,
        job_queue=job_queue,
        engine_manager=engine_manager,
    )
    return JobOut.model_validate(job)


@router.post("/{document_id}/pause", response_model=JobOut)
async def pause(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    source = await get_source(session, source_id)
    job = await pause_document(session, source, document_id)
    return JobOut.model_validate(job)


@router.post("/{document_id}/resume", response_model=JobOut)
async def resume(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> JobOut:
    source = await get_source(session, source_id)
    job = await resume_document(session, source, document_id, job_queue=job_queue)
    return JobOut.model_validate(job)


@router.delete("/{document_id}", response_model=Ok)
async def delete_(
    source_id: str,
    document_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> Ok:
    source = await get_source(session, source_id)
    await delete_document(
        session,
        source,
        document_id,
        engine_manager=engine_manager,
        job_queue=job_queue,
    )
    return Ok(detail="文档已删除")
