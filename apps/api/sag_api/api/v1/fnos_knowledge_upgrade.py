"""Per-tenant Native knowledge rebuild controls after the 0.13 engine upgrade."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_job_queue
from sag_api.core.errors import ConflictError, NotFoundError
from sag_api.db.models import User
from sag_api.fnos.knowledge_upgrade import (
    queue_legacy_reingest,
    reingest_status,
    replace_missing_original,
)
from sag_api.jobs import JobQueue
from sag_api.services.document_validation import validate_document_file

router = APIRouter(prefix="/fnos/knowledge-upgrade", tags=["fnos-knowledge-upgrade"])


def _require_native() -> None:
    if settings.auth_mode != "fnos":
        raise NotFoundError("此功能仅用于飞牛 Native 知识升级")


@router.get("")
async def status(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require_native()
    return await reingest_status(session)


@router.post("/reingest")
async def reingest(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> dict[str, int]:
    _require_native()
    return await queue_legacy_reingest(session, Path(settings.upload_dir), job_queue)


@router.post("/documents/{document_id}/original")
async def upload_missing_original(
    document_id: str,
    file: UploadFile = File(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    _require_native()
    validate_document_file(file.filename, 1, settings)
    data = await file.read()
    validate_document_file(file.filename, len(data), settings)
    try:
        await replace_missing_original(
            session,
            document_id,
            filename=file.filename or "",
            content_type=file.content_type or "application/octet-stream",
            data=data,
            uploads_dir=Path(settings.upload_dir),
        )
    except ValueError as error:
        raise ConflictError(str(error)) from error
    return {"uploaded": True}
