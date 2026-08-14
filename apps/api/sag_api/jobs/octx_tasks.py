from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.errors import NotFoundError
from sag_api.db.models import Job, OctxTransfer
from sag_api.db.models.octx import transition_transfer
from sag_api.enums import OctxTransferStatus
from sag_api.octx.runner import OctxRunner
from sag_api.services.octx_diagnostics_service import append_octx_trace
from sag_api.services.octx_gc_service import gc_expired_transfers, gc_installation
from sag_api.services.octx_transfer_service import (
    default_octx_storage,
    execute_export,
    execute_import,
    preflight_import,
)
from sag_api.services.source_operation_service import acquire_transfer_operation_lease

log = logging.getLogger(__name__)


async def _record_transfer_error(
    session: AsyncSession,
    transfer_id: str,
    error: Exception,
    *,
    retry_status: OctxTransferStatus,
    job_id: str | None = None,
) -> None:
    await session.rollback()
    transfer = await session.get(OctxTransfer, transfer_id)
    if transfer is None:
        return
    retryable = bool(getattr(error, "retryable", False))
    if retryable:
        transfer.status = retry_status
    elif transfer.status not in {
        OctxTransferStatus.READY,
        OctxTransferStatus.FAILED,
        OctxTransferStatus.CANCELLED,
        OctxTransferStatus.EXPIRED,
    }:
        transition_transfer(transfer, OctxTransferStatus.FAILED)
    error_payload = {
        "code": str(getattr(error, "code", "internal_error")),
        "message": str(getattr(error, "message", None) or error),
        "retryable": retryable,
    }
    details = getattr(error, "details", None)
    layer = getattr(error, "layer", None)
    stage = getattr(error, "stage", None)
    if isinstance(details, dict):
        error_payload["layer"] = str(getattr(layer, "value", layer))
        error_payload["stage"] = str(getattr(stage, "value", stage))
        error_payload["details"] = details
    transfer.error = error_payload
    progress_detail = dict((transfer.checkpoint or {}).get("progress_detail") or {})
    trace_stage = str(
        (str(getattr(stage, "value", stage)) if stage is not None else "")
        or progress_detail.get("kind")
        or progress_detail.get("phase")
        or "octx_task"
    )
    append_octx_trace(
        transfer,
        stage=trace_stage,
        state="retrying" if retryable else "failed",
        details={
            "job_id": job_id,
            "source_id": transfer.target_source_id,
            "code": error_payload["code"],
            "error_type": error.__class__.__name__,
        },
    )
    await session.commit()


async def preflight_octx(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    transfer_id = str((job.payload or {}).get("transfer_id") or "")
    transfer = await session.get(OctxTransfer, transfer_id) if transfer_id else None
    if transfer is None:
        raise NotFoundError("OCTX transfer not found")
    if job_queue is None:
        raise RuntimeError("OCTX preflight requires a job queue")
    try:
        await preflight_import(
            session,
            transfer,
            storage=default_octx_storage(),
            runner=OctxRunner(settings),
            job_queue=job_queue,
        )
    except Exception as error:
        await _record_transfer_error(
            session,
            transfer.id,
            error,
            retry_status=OctxTransferStatus.VALIDATING,
            job_id=job.id,
        )
        log.exception(
            "OCTX preflight failed transfer=%s job=%s source=%s stage=%s code=%s",
            transfer.id,
            job.id,
            transfer.target_source_id,
            getattr(getattr(error, "stage", None), "value", "preflight"),
            getattr(error, "code", "internal_error"),
        )
        raise
    job.progress = 1.0


async def export_octx(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    transfer_id = str((job.payload or {}).get("transfer_id") or "")
    transfer = await session.get(OctxTransfer, transfer_id) if transfer_id else None
    if transfer is None:
        raise NotFoundError("OCTX transfer not found")
    if transfer.cancellation_requested or transfer.status is OctxTransferStatus.CANCELLED:
        job.progress = 1.0
        return
    if not transfer.target_source_id:
        raise RuntimeError("OCTX export has no source")
    try:
        append_octx_trace(
            transfer,
            stage="queued",
            state="started",
            details={"job_id": job.id, "source_id": transfer.target_source_id},
        )
        await session.commit()
        async with acquire_transfer_operation_lease(
            SessionLocal,
            [f"source:{transfer.target_source_id}"],
            owner=f"{transfer.id}:{job.id}",
            drain_source_ids=[transfer.target_source_id],
        ):
            await execute_export(
                session,
                transfer,
                storage=default_octx_storage(),
                runner=OctxRunner(settings),
                engine_manager=engine_manager,
                attempt=max(1, job.attempts),
            )
    except Exception as error:
        await _record_transfer_error(
            session,
            transfer_id,
            error,
            retry_status=OctxTransferStatus.QUEUED,
            job_id=job.id,
        )
        log.exception(
            "OCTX export failed transfer=%s job=%s source=%s stage=%s code=%s",
            transfer_id,
            job.id,
            transfer.target_source_id,
            getattr(getattr(error, "stage", None), "value", "export"),
            getattr(error, "code", "internal_error"),
        )
        raise
    job.progress = 1.0


async def import_octx(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    transfer_id = str((job.payload or {}).get("transfer_id") or "")
    transfer = await session.get(OctxTransfer, transfer_id) if transfer_id else None
    if transfer is None:
        raise NotFoundError("OCTX transfer not found")
    if transfer.cancellation_requested or transfer.status is OctxTransferStatus.CANCELLED:
        job.progress = 1.0
        return
    resources = [f"asset:{transfer.asset_id}"]
    if transfer.target_source_id:
        resources.append(f"source:{transfer.target_source_id}")
    try:
        append_octx_trace(
            transfer,
            stage="queued",
            state="started",
            details={"job_id": job.id, "source_id": transfer.target_source_id},
        )
        await session.commit()
        async with acquire_transfer_operation_lease(
            SessionLocal,
            resources,
            owner=f"{transfer.id}:{job.id}",
            drain_source_ids=([transfer.target_source_id] if transfer.target_source_id else []),
        ):
            await execute_import(
                session,
                transfer,
                storage=default_octx_storage(),
                engine_manager=engine_manager,
                attempt=max(1, job.attempts),
            )
    except Exception as error:
        await _record_transfer_error(
            session,
            transfer_id,
            error,
            retry_status=OctxTransferStatus.QUEUED,
            job_id=job.id,
        )
        log.exception(
            "OCTX import failed transfer=%s job=%s source=%s stage=%s code=%s",
            transfer_id,
            job.id,
            transfer.target_source_id,
            getattr(getattr(error, "stage", None), "value", "import"),
            getattr(error, "code", "internal_error"),
        )
        raise
    job.progress = 1.0


async def gc_octx_installation(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    installation_id = str((job.payload or {}).get("installation_id") or "")
    if not installation_id:
        raise NotFoundError("OCTX installation not found")
    await gc_installation(
        session,
        installation_id,
        engine_manager=engine_manager,
    )
    job.progress = 1.0


async def gc_octx_transfers(session: AsyncSession, job: Job, *, engine_manager=None, job_queue=None) -> None:
    await gc_expired_transfers(session, storage=default_octx_storage())
    job.progress = 1.0
