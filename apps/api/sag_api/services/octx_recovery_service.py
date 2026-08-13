from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.db.models import (
    Job,
    OctxInstallation,
    OctxOperationLease,
    OctxTransfer,
    Source,
)
from sag_api.enums import (
    JobStatus,
    JobType,
    OctxInstallationStatus,
    OctxTransferStatus,
)

_OCTX_JOB_TYPES = {
    JobType.OCTX_PREFLIGHT,
    JobType.OCTX_IMPORT,
    JobType.OCTX_EXPORT,
    JobType.OCTX_GC_INSTALLATION,
    JobType.OCTX_GC_TRANSFER,
}
_RECOVERABLE = {
    OctxTransferStatus.VALIDATING,
    OctxTransferStatus.QUEUED,
    OctxTransferStatus.IMPORTING,
    OctxTransferStatus.INDEXING,
    OctxTransferStatus.SWITCHING,
    OctxTransferStatus.EXPORTING,
    OctxTransferStatus.PACKAGING,
}


async def recover_octx_state(session: AsyncSession) -> dict[str, int]:
    """Reconcile durable transfer state before the JobQueue re-enqueues work."""
    now = datetime.now(UTC)
    removed = await session.execute(
        delete(OctxOperationLease).where(OctxOperationLease.expires_at <= now)
    )
    jobs = (
        (
            await session.execute(
                select(Job)
                .where(
                    Job.type.in_(_OCTX_JOB_TYPES),
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .order_by(Job.created_at)
            )
        )
        .scalars()
        .all()
    )
    transfer_job_types = {
        JobType.OCTX_PREFLIGHT,
        JobType.OCTX_IMPORT,
        JobType.OCTX_EXPORT,
    }
    transfer_ids = {
        str((job.payload or {}).get("transfer_id"))
        for job in jobs
        if job.type in transfer_job_types and (job.payload or {}).get("transfer_id")
    }
    terminal_transfers = {
        transfer.id: transfer
        for transfer in (
            (
                await session.execute(
                    select(OctxTransfer).where(OctxTransfer.id.in_(transfer_ids))
                )
            )
            .scalars()
            .all()
            if transfer_ids
            else []
        )
        if transfer.status
        in {
            OctxTransferStatus.READY,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
            OctxTransferStatus.EXPIRED,
        }
    }
    for job in jobs:
        transfer_id = str((job.payload or {}).get("transfer_id") or "")
        terminal = terminal_transfers.get(transfer_id)
        if terminal is None:
            continue
        job.finished_at = now
        if terminal.status is OctxTransferStatus.READY:
            job.status = JobStatus.SUCCEEDED
            job.progress = 1.0
            job.error = None
        else:
            job.status = JobStatus.FAILED
            job.error = str(
                (terminal.error or {}).get("message")
                or f"OCTX transfer ended as {terminal.status.value}"
            )
    exhausted_by_transfer = {
        str((job.payload or {}).get("transfer_id")): job
        for job in jobs
        if job.type in transfer_job_types
        and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        and job.attempts >= settings.job_max_attempts
        and (job.payload or {}).get("transfer_id")
    }
    for job in exhausted_by_transfer.values():
        job.status = JobStatus.FAILED
        job.finished_at = now
        job.error = f"OCTX task exceeded {settings.job_max_attempts} recovery attempts"
    jobs_by_transfer = {
        str((job.payload or {}).get("transfer_id")): job
        for job in jobs
        if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        and (job.payload or {}).get("transfer_id")
    }
    transfers = (
        (
            await session.execute(
                select(OctxTransfer).where(
                    OctxTransfer.status.not_in(
                        [
                            OctxTransferStatus.READY,
                            OctxTransferStatus.FAILED,
                            OctxTransferStatus.CANCELLED,
                            OctxTransferStatus.EXPIRED,
                        ]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    requeued = 0
    expired = 0
    for transfer in transfers:
        deadline = (
            transfer.decision_expires_at
            if transfer.status is OctxTransferStatus.DECISION_REQUIRED
            else transfer.expires_at
        )
        if deadline is not None and deadline <= now and transfer.status in {
            OctxTransferStatus.UPLOADED,
            OctxTransferStatus.DECISION_REQUIRED,
        }:
            transfer.status = OctxTransferStatus.EXPIRED
            expired += 1
            continue
        if transfer.status not in _RECOVERABLE:
            continue
        if transfer.id in exhausted_by_transfer:
            transfer.status = OctxTransferStatus.FAILED
            transfer.error = {
                "code": "octx_recovery_attempts_exhausted",
                "message": "导出进程多次异常退出，任务已停止自动恢复，请重试导出",
                "retryable": True,
            }
            continue
        job = jobs_by_transfer.get(transfer.id)
        if job is None:
            transfer.status = OctxTransferStatus.FAILED
            transfer.error = {
                "code": "octx_recovery_job_missing",
                "message": "unfinished OCTX transfer has no recoverable job",
                "retryable": False,
            }
            continue
        transfer.status = (
            OctxTransferStatus.VALIDATING
            if job.type is JobType.OCTX_PREFLIGHT
            else OctxTransferStatus.QUEUED
        )
        requeued += 1

    gc_scheduled = 0
    active_installation_gc = {
        str((job.payload or {}).get("installation_id"))
        for job in jobs
        if job.type is JobType.OCTX_GC_INSTALLATION
        and (job.payload or {}).get("installation_id")
    }
    candidates = (
        (
            await session.execute(
                select(OctxInstallation, Source.sag_source_config_id)
                .join(Source, Source.id == OctxInstallation.source_id)
                .where(
                    or_(
                        and_(
                            OctxInstallation.status
                            == OctxInstallationStatus.RETAINED,
                            OctxInstallation.retain_until.is_not(None),
                            OctxInstallation.retain_until <= now,
                        ),
                        OctxInstallation.status == OctxInstallationStatus.FAILED,
                    )
                )
            )
        )
        .all()
    )
    for installation, current_source_config_id in candidates:
        if (
            installation.id in active_installation_gc
            or installation.sag_source_config_id == current_source_config_id
        ):
            continue
        session.add(
            Job(
                type=JobType.OCTX_GC_INSTALLATION,
                status=JobStatus.QUEUED,
                source_id=installation.source_id,
                payload={"installation_id": installation.id},
            )
        )
        gc_scheduled += 1

    has_transfer_gc = any(job.type is JobType.OCTX_GC_TRANSFER for job in jobs)
    expired_staging = await session.scalar(
        select(OctxTransfer.id)
        .where(
            OctxTransfer.status.in_(
                [
                    OctxTransferStatus.READY,
                    OctxTransferStatus.FAILED,
                    OctxTransferStatus.CANCELLED,
                    OctxTransferStatus.EXPIRED,
                ]
            ),
            OctxTransfer.expires_at.is_not(None),
            OctxTransfer.expires_at <= now,
            OctxTransfer.staging_key.is_not(None),
        )
        .limit(1)
    )
    if expired_staging is not None and not has_transfer_gc:
        session.add(
            Job(
                type=JobType.OCTX_GC_TRANSFER,
                status=JobStatus.QUEUED,
                payload={},
            )
        )
        gc_scheduled += 1
    await session.commit()
    return {
        "requeued": requeued,
        "expired": expired,
        "leases_removed": int(removed.rowcount or 0),
        "gc_scheduled": gc_scheduled,
    }
