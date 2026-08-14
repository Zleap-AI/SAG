from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import get_current_user, get_job_queue
from sag_api.core.errors import ConflictError, ForbiddenError, NotFoundError
from sag_api.db.models import OctxRelease, OctxSourceBinding, OctxTransfer, User
from sag_api.db.models.octx import transition_transfer
from sag_api.enums import OctxTransferStatus
from sag_api.jobs import JobQueue
from sag_api.schemas.octx import (
    OctxExportCreate,
    OctxExportDecisionIn,
    OctxImportDecisionIn,
    OctxTransferOut,
)
from sag_api.services.octx_conflict_service import ImportDecision
from sag_api.services.octx_diagnostics_service import build_octx_diagnostic_snapshot
from sag_api.services.octx_transfer_service import (
    create_document_export_transfer,
    create_export_transfer,
    create_import_transfer,
    default_octx_storage,
    submit_export_decision,
    submit_import_decision,
)
from sag_api.services.source_operation_service import export_request_admission

router = APIRouter(tags=["octx"])


async def _transfer(session: AsyncSession, transfer_id: str) -> OctxTransfer:
    transfer = await session.get(OctxTransfer, transfer_id)
    if transfer is None:
        raise NotFoundError("OCTX transfer not found")
    return transfer


@router.post(
    "/octx/imports", response_model=OctxTransferOut, status_code=202
)
async def create_import(
    file: UploadFile = File(...),
    transfer_id: str | None = Header(default=None, alias="X-OCTX-Transfer-ID"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> OctxTransferOut:
    transfer = await create_import_transfer(
        session,
        file,
        storage=default_octx_storage(),
        job_queue=job_queue,
        transfer_id=transfer_id,
        requested_by_user_id=user.id,
    )
    return OctxTransferOut.from_transfer(transfer)


@router.post(
    "/sources/{source_id}/documents/{document_id}/octx-exports",
    response_model=OctxTransferOut,
    status_code=202,
)
async def create_document_export(
    source_id: str,
    document_id: str,
    body: OctxExportCreate,
    transfer_id: str | None = Header(default=None, alias="X-OCTX-Transfer-ID"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> OctxTransferOut:
    async with export_request_admission(SessionLocal, source_id):
        transfer = await create_document_export_transfer(
            session,
            source_id,
            document_id,
            version=body.version,
            job_queue=job_queue,
            transfer_id=transfer_id,
            requested_by_user_id=user.id,
        )
    return OctxTransferOut.from_transfer(transfer)


@router.get("/octx/transfers/{transfer_id}", response_model=OctxTransferOut)
async def get_transfer(
    transfer_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OctxTransferOut:
    return OctxTransferOut.from_transfer(await _transfer(session, transfer_id))


@router.get("/octx/transfers/{transfer_id}/diagnostics")
async def get_transfer_diagnostics(
    transfer_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    transfer = await _transfer(session, transfer_id)
    owner_id = str((transfer.checkpoint or {}).get("requested_by_user_id") or "")
    if not owner_id or owner_id != user.id:
        raise ForbiddenError("OCTX diagnostic bundle is not available for this user")
    return await build_octx_diagnostic_snapshot(session, transfer_id)


@router.post(
    "/octx/imports/{transfer_id}/decision", response_model=OctxTransferOut
)
async def decide_import(
    transfer_id: str,
    body: OctxImportDecisionIn,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> OctxTransferOut:
    transfer = await submit_import_decision(
        session,
        transfer_id,
        ImportDecision(
            action=body.action,
            target_source_id=body.target_source_id,
            discard_local_changes=body.discard_local_changes,
            decision_token=body.decision_token,
        ),
        job_queue=job_queue,
    )
    return OctxTransferOut.from_transfer(transfer)


@router.get("/octx/transfers/{transfer_id}/artifact")
async def download_artifact(
    transfer_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    transfer = await _transfer(session, transfer_id)
    if transfer.status is not OctxTransferStatus.READY or not transfer.artifact_key:
        raise ConflictError("OCTX artifact is not ready")
    storage = default_octx_storage()
    path = storage.resolve_key(transfer.artifact_key)
    if not path.is_file():
        raise NotFoundError("OCTX artifact is missing")
    digest = str(transfer.package_digest or "")
    return FileResponse(
        path,
        media_type="application/vnd.octx+zip",
        filename=(
            f"{str((transfer.checkpoint or {}).get('asset_name') or transfer.asset_id or transfer.id)}"
            f"-{transfer.package_version or 'release'}.octx"
        ),
        headers={"ETag": f'"{digest}"', "Digest": digest},
    )


@router.post(
    "/sources/{source_id}/octx-exports",
    response_model=OctxTransferOut,
    status_code=202,
)
async def create_export(
    source_id: str,
    body: OctxExportCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> OctxTransferOut:
    async with export_request_admission(SessionLocal, source_id):
        transfer = await create_export_transfer(
            session,
            source_id,
            version=body.version,
            job_queue=job_queue,
            requested_by_user_id=user.id,
        )
    return OctxTransferOut.from_transfer(transfer)


@router.post(
    "/octx/exports/{transfer_id}/decision", response_model=OctxTransferOut
)
async def decide_export(
    transfer_id: str,
    body: OctxExportDecisionIn,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    job_queue: JobQueue = Depends(get_job_queue),
) -> OctxTransferOut:
    transfer = await submit_export_decision(
        session,
        transfer_id,
        action=body.action,
        decision_token=body.decision_token,
        job_queue=job_queue,
    )
    return OctxTransferOut.from_transfer(transfer)


@router.get("/sources/{source_id}/octx-releases")
async def list_releases(
    source_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    from sqlalchemy import select

    binding = await session.get(OctxSourceBinding, source_id)
    if binding is None:
        return []
    releases = (
        (
            await session.execute(
                select(OctxRelease)
                .where(OctxRelease.asset_id == binding.asset_id)
                .order_by(OctxRelease.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": release.id,
            "asset_id": release.asset_id,
            "version": release.version,
            "package_digest": release.package_digest,
            "active": release.id == binding.active_release_id,
            "created_at": release.created_at,
        }
        for release in releases
    ]


@router.delete("/octx/transfers/{transfer_id}", response_model=OctxTransferOut)
async def cancel_transfer(
    transfer_id: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OctxTransferOut:
    transfer = await _transfer(session, transfer_id)
    if transfer.status is OctxTransferStatus.SWITCHING:
        raise ConflictError("OCTX transfer can no longer be cancelled while switching")
    if transfer.status in {
        OctxTransferStatus.READY,
        OctxTransferStatus.FAILED,
        OctxTransferStatus.EXPIRED,
    }:
        raise ConflictError("OCTX transfer is already terminal")
    if transfer.status is not OctxTransferStatus.CANCELLED:
        transfer.cancellation_requested = True
        transition_transfer(transfer, OctxTransferStatus.CANCELLED)
        await session.commit()
    return OctxTransferOut.from_transfer(transfer)
