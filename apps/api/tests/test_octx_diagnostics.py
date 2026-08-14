from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def diagnostic_sessions(tmp_path):
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


def test_octx_trace_is_bounded_and_sanitized():
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_diagnostics_service import append_octx_trace

    transfer = OctxTransfer(
        direction=OctxTransferDirection.EXPORT,
        status=OctxTransferStatus.QUEUED,
        checkpoint={},
    )

    for index in range(105):
        append_octx_trace(
            transfer,
            stage="snapshot_structured",
            state="running",
            details={
                "index": index,
                "content": "private document body",
                "storage_path": "/Users/private/source.pdf",
                "api_token": "top-secret-token",
                "safe": "x" * 3000,
            },
        )

    trace = transfer.checkpoint["diagnostic_trace"]
    serialized = json.dumps(trace)
    assert len(trace) == 100
    assert trace[0]["details"]["index"] == 5
    assert trace[-1]["details"]["index"] == 104
    assert "private document body" not in serialized
    assert "/Users/private/source.pdf" not in serialized
    assert "top-secret-token" not in serialized
    assert len(trace[-1]["details"]["safe"]) <= 2000


@pytest.mark.asyncio
async def test_octx_diagnostic_snapshot_correlates_job_and_environment(
    diagnostic_sessions,
):
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import (
        JobStatus,
        JobType,
        OctxTransferDirection,
        OctxTransferStatus,
    )
    from sag_api.services.octx_diagnostics_service import (
        build_octx_diagnostic_snapshot,
    )

    async with diagnostic_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.FAILED,
            checkpoint={"progress_detail": {"stage": "package_validation"}},
            error={
                "code": "octx_validation_failed",
                "message": "package validation failed",
                "details": {"api_key": "must-not-leak"},
            },
        )
        session.add(transfer)
        await session.flush()
        job = Job(
            type=JobType.OCTX_EXPORT,
            status=JobStatus.FAILED,
            payload={"transfer_id": transfer.id},
            error="Traceback at /Users/private/SAG/octx.py: validation failed",
            attempts=2,
        )
        session.add(job)
        await session.commit()

        snapshot = await build_octx_diagnostic_snapshot(session, transfer.id)

    serialized = json.dumps(snapshot)
    assert snapshot["transfer"]["id"] == transfer.id
    assert snapshot["transfer"]["direction"] == "export"
    assert snapshot["transfer"]["status"] == "failed"
    assert snapshot["jobs"][0]["id"] == job.id
    assert snapshot["jobs"][0]["attempts"] == 2
    assert snapshot["environment"]["python"]
    assert snapshot["environment"]["platform"]
    assert snapshot["environment"]["octx_version"]
    assert "database_backend" in snapshot["environment"]
    assert "storage" in snapshot["environment"]
    assert "must-not-leak" not in serialized
    assert "/Users/" not in serialized


@pytest.mark.asyncio
async def test_octx_diagnostic_snapshot_includes_preflight_job(diagnostic_sessions):
    from sag_api.db.models import Job, OctxTransfer
    from sag_api.enums import JobStatus, JobType, OctxTransferDirection, OctxTransferStatus
    from sag_api.services.octx_diagnostics_service import build_octx_diagnostic_snapshot

    async with diagnostic_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.IMPORT,
            status=OctxTransferStatus.FAILED,
            checkpoint={},
        )
        session.add(transfer)
        await session.flush()
        preflight = Job(
            type=JobType.OCTX_PREFLIGHT,
            status=JobStatus.FAILED,
            payload={"transfer_id": transfer.id},
            error="invalid manifest",
            attempts=1,
        )
        session.add(preflight)
        await session.commit()

        snapshot = await build_octx_diagnostic_snapshot(session, transfer.id)

    assert [job["id"] for job in snapshot["jobs"]] == [preflight.id]
    assert snapshot["jobs"][0]["type"] == "octx_preflight"


@pytest.mark.asyncio
async def test_document_export_binding_is_independent_from_source_binding(
    diagnostic_sessions,
):
    from sag_api.db.models import (
        Document,
        OctxAsset,
        OctxDocumentBinding,
        OctxRelease,
        Source,
    )
    from sag_api.enums import (
        DocumentStatus,
        OctxAssetOwnership,
        OctxReleaseOrigin,
        SourceStatus,
        SourceType,
    )

    async with diagnostic_sessions() as session:
        source = Source(
            name="source",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="source-config",
            status=SourceStatus.ACTIVE,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="one.pdf",
            storage_path="one.pdf",
            status=DocumentStatus.READY,
            sag_source_id="article-1",
        )
        session.add(document)
        await session.flush()
        asset = OctxAsset(
            id="0198f12d-80c0-7000-8000-000000000001",
            name="one.pdf",
            ownership=OctxAssetOwnership.LOCAL,
            producer_source_id=source.id,
        )
        session.add(asset)
        await session.flush()
        release = OctxRelease(
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest={},
            artifact_key="releases/one.octx",
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add(release)
        await session.flush()
        session.add(
            OctxDocumentBinding(
                document_id=document.id,
                asset_id=asset.id,
                active_release_id=release.id,
                content_revision=1,
                released_revision=1,
                workspace_key=f"document-workspaces/{document.id}",
            )
        )
        await session.commit()

        binding = await session.get(OctxDocumentBinding, document.id)
        assert binding is not None
        assert binding.asset_id == asset.id
        assert binding.active_release_id == release.id


@pytest.mark.asyncio
async def test_octx_diagnostics_require_the_transfer_creator(diagnostic_sessions):
    from types import SimpleNamespace

    from sag_api.api.v1.octx import get_transfer_diagnostics
    from sag_api.core.errors import ForbiddenError
    from sag_api.db.models import OctxTransfer
    from sag_api.enums import OctxTransferDirection, OctxTransferStatus

    async with diagnostic_sessions() as session:
        transfer = OctxTransfer(
            direction=OctxTransferDirection.EXPORT,
            status=OctxTransferStatus.FAILED,
            checkpoint={"requested_by_user_id": "user-1"},
        )
        session.add(transfer)
        await session.commit()

        snapshot = await get_transfer_diagnostics(
            transfer.id,
            SimpleNamespace(id="user-1"),
            session,
        )
        assert snapshot["transfer"]["id"] == transfer.id

        with pytest.raises(ForbiddenError):
            await get_transfer_diagnostics(
                transfer.id,
                SimpleNamespace(id="user-2"),
                session,
            )
