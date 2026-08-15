from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.errors import ConflictError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import ConnectorKind, JobStatus, JobType, SourceType
from sag_api.services.fnos_nas_import import NasImportOutcome


class FakeQueue:
    pass


class FakeImporter:
    def __init__(self, outcomes: list[NasImportOutcome | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def import_one(self, _session, _job, entry, *, identity):
        assert identity.uid == 1000
        assert identity.is_admin is True
        self.calls.append(entry.display_path)
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _entry(name: str) -> dict[str, object]:
    return {
        "canonical_root": "/volume1/shared",
        "canonical_path": f"/volume1/shared/{name}",
        "display_path": name,
        "size_bytes": 10,
        "mtime_ns": 123,
        "folder_source": "host_api",
    }


@pytest.fixture
async def import_job_db() -> tuple[AsyncSession, Job]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(Job.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        source = Source(
            name="Private",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="source-config",
        )
        session.add(source)
        await session.flush()
        job = Job(
            type=JobType.IMPORT_NAS_DOCUMENTS,
            status=JobStatus.RUNNING,
            source_id=source.id,
            payload={
                "owner_uid": 1000,
                "entries": [_entry("a.pdf"), _entry("b.pdf"), _entry("c.pdf")],
                "summary": {"total": 3, "completed": 0},
                "results": [],
            },
        )
        session.add(job)
        await session.commit()
        yield session, job
    await engine.dispose()


@pytest.mark.asyncio
async def test_import_job_checkpoints_each_item_and_projects_safe_results(
    monkeypatch, import_job_db: tuple[AsyncSession, Job]
) -> None:
    from sag_api.jobs import tasks

    session, job = import_job_db
    importer = FakeImporter(
        [
            NasImportOutcome("a.pdf", "created", "doc-a"),
            NasImportOutcome("b.pdf", "skipped", "doc-b"),
            ConflictError("sensitive path", code="nas_file_unreadable"),
        ]
    )
    monkeypatch.setattr(tasks, "_build_fnos_nas_importer", lambda _queue: importer)

    await tasks.import_nas_documents(session, job, job_queue=FakeQueue())

    await session.refresh(job)
    assert job.progress == 1.0
    assert job.payload["summary"] == {
        "total": 3,
        "completed": 3,
        "created": 1,
        "updated": 0,
        "skipped": 1,
        "failed": 1,
    }
    assert job.payload["results"] == [
        {
            "display_path": "a.pdf",
            "outcome": "created",
            "document_id": "doc-a",
            "reason": None,
        },
        {
            "display_path": "b.pdf",
            "outcome": "skipped",
            "document_id": "doc-b",
            "reason": None,
        },
        {
            "display_path": "c.pdf",
            "outcome": "failed",
            "document_id": None,
            "reason": "file_unreadable",
        },
    ]
    assert all("canonical_path" not in result for result in job.payload["results"])
    assert [entry["state"] for entry in job.payload["entries"]] == [
        "created",
        "skipped",
        "failed",
    ]


@pytest.mark.asyncio
async def test_import_job_retry_skips_terminal_entries_and_retries_copying_entry(
    monkeypatch, import_job_db: tuple[AsyncSession, Job]
) -> None:
    from sag_api.jobs import tasks

    session, job = import_job_db
    payload = dict(job.payload)
    entries = list(payload["entries"])
    entries[0] = {
        **entries[0],
        "state": "created",
        "document_id": "doc-a",
        "reason": None,
    }
    entries[1] = {**entries[1], "state": "copying"}
    entries[2] = {
        **entries[2],
        "state": "failed",
        "document_id": None,
        "reason": "file_changed",
    }
    job.payload = {**payload, "entries": entries}
    await session.commit()
    importer = FakeImporter([NasImportOutcome("b.pdf", "updated", "doc-b")])
    monkeypatch.setattr(tasks, "_build_fnos_nas_importer", lambda _queue: importer)

    await tasks.import_nas_documents(session, job, job_queue=FakeQueue())

    await session.refresh(job)
    assert importer.calls == ["b.pdf"]
    assert job.payload["summary"] == {
        "total": 3,
        "completed": 3,
        "created": 1,
        "updated": 1,
        "skipped": 0,
        "failed": 1,
    }
    assert [result["outcome"] for result in job.payload["results"]] == [
        "created",
        "updated",
        "failed",
    ]


def test_import_job_is_registered() -> None:
    from sag_api.jobs.tasks import TASK_HANDLERS, import_nas_documents

    assert TASK_HANDLERS[JobType.IMPORT_NAS_DOCUMENTS] is import_nas_documents


@pytest.mark.asyncio
async def test_import_job_rejects_invalid_persisted_entry(
    monkeypatch, import_job_db: tuple[AsyncSession, Job]
) -> None:
    from sag_api.jobs import tasks

    session, job = import_job_db
    job.payload = {**job.payload, "entries": [{"display_path": "unsafe.pdf"}]}
    await session.commit()
    monkeypatch.setattr(
        tasks,
        "_build_fnos_nas_importer",
        lambda _queue: FakeImporter([]),
    )

    await tasks.import_nas_documents(session, job, job_queue=FakeQueue())

    await session.refresh(job)
    assert job.payload["summary"]["failed"] == 1
    assert job.payload["results"] == [
        {
            "display_path": "unsafe.pdf",
            "outcome": "failed",
            "document_id": None,
            "reason": "unsafe_or_unsupported",
        }
    ]


@pytest.mark.asyncio
async def test_stage_recovery_only_removes_terminal_and_stale_owned_directories(
    monkeypatch,
    tmp_path: Path,
    import_job_db: tuple[AsyncSession, Job],
) -> None:
    from sag_api.core.config import settings
    from sag_api.jobs.inproc import _reconcile_nas_stage_directories

    session, active = import_job_db
    active.status = JobStatus.QUEUED
    terminal = Job(
        type=JobType.IMPORT_NAS_DOCUMENTS,
        status=JobStatus.SUCCEEDED,
        source_id=active.source_id,
        payload={},
    )
    referenced_batch = Job(
        type=JobType.IMPORT_NAS_DOCUMENTS,
        status=JobStatus.SUCCEEDED,
        source_id=active.source_id,
        payload={},
    )
    session.add_all([terminal, referenced_batch])
    await session.flush()
    root = tmp_path / ".nas-stage"
    active_dir = root / active.id
    terminal_dir = root / terminal.id
    referenced_dir = root / referenced_batch.id
    old_orphan_dir = root / ("a" * 32)
    recent_orphan_dir = root / ("b" * 32)
    unsafe_dir = root / "manual-files"
    for directory in [
        active_dir,
        terminal_dir,
        referenced_dir,
        old_orphan_dir,
        recent_orphan_dir,
        unsafe_dir,
    ]:
        directory.mkdir(parents=True)
        (directory / "item.stage").write_bytes(b"staged")
    replacement = Job(
        type=JobType.REPROCESS_DOCUMENT,
        status=JobStatus.QUEUED,
        source_id=active.source_id,
        payload={
            "replacement": {
                "state": "staged",
                "staged_path": str(referenced_dir / "item.stage"),
            }
        },
    )
    session.add(replacement)
    await session.commit()
    old = time.time() - 25 * 60 * 60
    os.utime(old_orphan_dir, (old, old))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    await _reconcile_nas_stage_directories(session, now=time.time())

    assert active_dir.exists()
    assert referenced_dir.exists()
    assert recent_orphan_dir.exists()
    assert unsafe_dir.exists()
    assert not terminal_dir.exists()
    assert not old_orphan_dir.exists()
