from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def job_sessions(tmp_path):
    from sag_api.db import models  # noqa: F401
    from sag_api.db.base import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retryable_import_failure_restores_transfer_to_queued(job_sessions, monkeypatch):
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import JobStatus, JobType, OctxTransferDirection, OctxTransferStatus
    from sag_api.jobs import octx_tasks

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield

    async def fail(session, transfer, **kwargs):
        transfer.status = OctxTransferStatus.IMPORTING
        await session.commit()
        raise ServiceUnavailableError("embedding temporarily unavailable")

    monkeypatch.setattr(octx_tasks, "acquire_transfer_operation_lease", lease)
    monkeypatch.setattr(octx_tasks, "execute_import", fail)
    async with job_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.QUEUED,
        )
        session.add(transfer)
        await session.flush()
        job = Job(
            type=JobType.OCTX_IMPORT,
            status=JobStatus.RUNNING,
            payload={"transfer_id": transfer.id},
        )
        session.add(job)
        await session.commit()

        with pytest.raises(ServiceUnavailableError):
            await octx_tasks.import_octx(session, job, engine_manager=object())
        await session.refresh(transfer)
        assert transfer.status is OctxTransferStatus.QUEUED
        assert transfer.error == {
            "code": "service_unavailable",
            "message": "embedding temporarily unavailable",
            "retryable": True,
        }


def test_job_queue_honors_domain_retryable_flag():
    """A busy OCTX resource must be retried instead of leaving its transfer queued forever."""
    from sag_api.core.errors import ConflictError
    from sag_api.jobs.inproc import _is_retryable

    assert _is_retryable(ConflictError("resource busy", retryable=True)) is True
    assert _is_retryable(ConflictError("permanent conflict", retryable=False)) is False


@pytest.mark.asyncio
async def test_cancelled_transfer_stops_before_export_side_effects(job_sessions, monkeypatch):
    """A cancelled export must not enter package construction or publication."""
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import JobStatus, JobType, OctxTransferDirection, OctxTransferStatus
    from sag_api.jobs import octx_tasks

    called = False

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield

    async def execute(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(octx_tasks, "acquire_transfer_operation_lease", lease)
    monkeypatch.setattr(octx_tasks, "execute_export", execute)
    async with job_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.CANCELLED,
            target_source_id="source-id",
            cancellation_requested=True,
        )
        session.add(transfer)
        await session.flush()
        job = Job(
            type=JobType.OCTX_EXPORT,
            status=JobStatus.RUNNING,
            payload={"transfer_id": transfer.id},
        )
        session.add(job)
        await session.commit()

        await octx_tasks.export_octx(session, job, engine_manager=object())

        assert called is False
        assert transfer.status is OctxTransferStatus.CANCELLED


@pytest.mark.asyncio
async def test_export_failure_preserves_reextract_recovery_details(job_sessions, monkeypatch):
    """Dropping domain details would leave the UI unable to identify recoverable documents."""
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import JobStatus, JobType, OctxTransferDirection, OctxTransferStatus
    from sag_api.jobs import octx_tasks
    from sag_api.octx.errors import OctxSourceReextractRequiredError

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield

    async def fail(*args, **kwargs):
        raise OctxSourceReextractRequiredError(
            [{"id": "doc-1", "filename": "legacy.md", "event_count": 2}],
            event_count=2,
        )

    monkeypatch.setattr(octx_tasks, "acquire_transfer_operation_lease", lease)
    monkeypatch.setattr(octx_tasks, "execute_export", fail)
    async with job_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.QUEUED,
            target_source_id="source-id",
        )
        session.add(transfer)
        await session.flush()
        job = Job(
            type=JobType.OCTX_EXPORT,
            status=JobStatus.RUNNING,
            payload={"transfer_id": transfer.id},
        )
        session.add(job)
        await session.commit()

        with pytest.raises(OctxSourceReextractRequiredError):
            await octx_tasks.export_octx(session, job, engine_manager=object())
        await session.refresh(transfer)

        assert transfer.status is OctxTransferStatus.FAILED
        assert transfer.error == {
            "code": "octx_source_reextract_required",
            "message": "部分文档的事项数据不完整，无法导出。请重新提取这些文档后再试。",
            "layer": "engine",
            "stage": "octx_export",
            "retryable": False,
            "details": {
                "documents": [{"id": "doc-1", "filename": "legacy.md", "event_count": 2}],
                "event_count": 2,
                "recovery_action": "reprocess_documents",
            },
        }
