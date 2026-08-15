"""Administrator-only fnOS NAS discovery and durable import submission routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import (
    get_current_user,
    get_fnos_identity,
    get_fnos_nas_access,
    get_fnos_nas_registry,
    get_fnos_nas_scanner,
    get_job_queue,
    require_fnos_nas_admin,
)
from sag_api.core.errors import NotFoundError, ValidationError
from sag_api.db.models import Job, User
from sag_api.enums import ConnectorKind, JobStatus, JobType
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import FnOSNasScanRegistry
from sag_api.jobs import JobQueue
from sag_api.schemas.fnos_nas import (
    NasFolderOut,
    NasImportAccepted,
    NasImportItemOut,
    NasImportProgressOut,
    NasImportRequest,
    NasLegacyFolderCreate,
    NasLimitsOut,
    NasScanOut,
    NasScanRequest,
    NasStatusOut,
)
from sag_api.services.document_service import _enqueue_persisted_job
from sag_api.services.fnos_nas_access import FnOSNasAccessService, NasMode
from sag_api.services.fnos_nas_scanner import SCAN_LIMITS, FnOSNasScanner
from sag_api.services.source_service import get_source

router = APIRouter(prefix="/fnos/nas", tags=["fnos-nas"])

MAX_IMPORT_FILES = 500
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024


def _limits() -> NasLimitsOut:
    return NasLimitsOut(
        max_files=int(SCAN_LIMITS["returned_files"]),
        max_import_files=MAX_IMPORT_FILES,
        max_import_bytes=MAX_IMPORT_BYTES,
        max_file_bytes=settings.max_upload_mb * 1024 * 1024,
    )


@router.get("/status", response_model=NasStatusOut)
async def get_status(
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(get_fnos_identity),
    session: AsyncSession = Depends(get_session),
    access: FnOSNasAccessService = Depends(get_fnos_nas_access),
) -> NasStatusOut:
    value = await access.status(session, identity, "zh-CN")
    return NasStatusOut(
        eligible=value.eligible,
        mode=value.mode,
        system_version=value.system_version,
        automatic_authorization=value.automatic_authorization,
        folders=[NasFolderOut.model_validate(folder) for folder in value.folders],
        limits=_limits(),
        reason=value.reason,
    )


@router.post("/legacy-folders", response_model=NasFolderOut, status_code=201)
async def register_legacy_folder(
    body: NasLegacyFolderCreate,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(require_fnos_nas_admin),
    session: AsyncSession = Depends(get_session),
    access: FnOSNasAccessService = Depends(get_fnos_nas_access),
) -> NasFolderOut:
    current = await access.status(session, identity, "zh-CN")
    if current.mode is not NasMode.LEGACY_MANUAL:
        raise ValidationError("当前系统无需手动登记授权目录")
    return NasFolderOut.model_validate(await access.register_legacy_folder(session, body.path))


@router.delete("/legacy-folders/{folder_id}", status_code=204)
async def delete_legacy_folder(
    folder_id: str,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(require_fnos_nas_admin),
    session: AsyncSession = Depends(get_session),
    access: FnOSNasAccessService = Depends(get_fnos_nas_access),
) -> Response:
    del identity
    await access.delete_legacy_folder(session, folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/scan", response_model=NasScanOut)
async def scan_folder(
    body: NasScanRequest,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(require_fnos_nas_admin),
    session: AsyncSession = Depends(get_session),
    access: FnOSNasAccessService = Depends(get_fnos_nas_access),
    scanner: FnOSNasScanner = Depends(get_fnos_nas_scanner),
) -> NasScanOut:
    source = await get_source(session, body.source_id)
    if source.connector_kind is not ConnectorKind.FILE_UPLOAD:
        raise ValidationError("该信源不支持 NAS 文档导入")
    root = await access.resolve_root(session, identity, body.folder_id)
    result = await scanner.scan(
        session,
        identity=identity,
        source=source,
        root=root,
        recursive=body.recursive,
    )
    return NasScanOut.model_validate(result)


@router.post("/imports", response_model=NasImportAccepted, status_code=202)
async def create_import(
    body: NasImportRequest,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(require_fnos_nas_admin),
    session: AsyncSession = Depends(get_session),
    registry: FnOSNasScanRegistry = Depends(get_fnos_nas_registry),
    job_queue: JobQueue = Depends(get_job_queue),
) -> NasImportAccepted:
    source = await get_source(session, body.source_id)
    if source.connector_kind is not ConnectorKind.FILE_UPLOAD:
        raise ValidationError("该信源不支持 NAS 文档导入")
    entries = registry.resolve_many(identity.uid, source.id, body.selection_tokens)
    if len(entries) > MAX_IMPORT_FILES or sum(entry.size_bytes for entry in entries) > MAX_IMPORT_BYTES:
        raise ValidationError("选择的 NAS 文档超过单次导入上限")
    job = Job(
        type=JobType.IMPORT_NAS_DOCUMENTS,
        status=JobStatus.QUEUED,
        source_id=source.id,
        payload={
            "owner_uid": identity.uid,
            "entries": [asdict(entry) for entry in entries],
            "summary": {
                "total": len(entries),
                "completed": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            },
            "results": [],
        },
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    await _enqueue_persisted_job(job_queue, job.id)
    return NasImportAccepted(job_id=job.id, accepted=len(entries))


@router.get("/imports/{job_id}", response_model=NasImportProgressOut)
async def get_import(
    job_id: str,
    _user: User = Depends(get_current_user),
    identity: GatewayIdentity = Depends(require_fnos_nas_admin),
    session: AsyncSession = Depends(get_session),
) -> NasImportProgressOut:
    job = await session.get(Job, job_id)
    if (
        job is None
        or job.type is not JobType.IMPORT_NAS_DOCUMENTS
        or job.payload.get("owner_uid") != identity.uid
        or not job.source_id
    ):
        raise NotFoundError("导入任务不存在")
    await get_source(session, job.source_id)
    raw_summary = job.payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_results = job.payload.get("results")
    results: list[NasImportItemOut] = []
    if isinstance(raw_results, list):
        for item in raw_results[:MAX_IMPORT_FILES]:
            if not isinstance(item, dict):
                continue
            results.append(
                NasImportItemOut(
                    display_path=item.get("display_path", ""),
                    outcome=item.get("outcome", "failed"),
                    document_id=item.get("document_id"),
                    reason=item.get("reason"),
                )
            )
    return NasImportProgressOut(
        id=job.id,
        status=job.status,
        progress=job.progress,
        total=summary.get("total", 0),
        completed=summary.get("completed", 0),
        created=summary.get("created", 0),
        updated=summary.get("updated", 0),
        skipped=summary.get("skipped", 0),
        failed=summary.get("failed", 0),
        results=results,
    )
