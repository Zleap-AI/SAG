from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.errors import ConflictError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import ConnectorKind, DocumentStatus, JobStatus, JobType, SourceType
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import NasScanEntry
from sag_api.fnos.open_api import UserACL
from sag_api.jobs import JobQueue
from sag_api.jobs.tasks import reprocess_document_task
from sag_api.services.fnos_nas_import import FnOSNasImporter
from sag_api.services.fnos_nas_scanner import nas_origin_key


class Queue(JobQueue):
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.maintenance: list[tuple[str, str]] = []

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)

    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        self.maintenance.append((source_id, job_id))

    def source_maintenance_requested(self, source_id: str) -> bool:
        return bool(self.maintenance)

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass


class Access:
    async def revalidate_root(self, *_args) -> None:
        return None


class Host:
    async def check_user_acl(self, _uid: int, paths: list[str]) -> list[UserACL]:
        return [UserACL(path=path, readable=True, writable=False, deletable=False) for path in paths]


class Engine:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_document_data(self, _config: str, derived: str, *, source) -> None:
        self.deleted.append(derived)


def _entry(root: Path, file: Path) -> NasScanEntry:
    metadata = file.stat()
    return NasScanEntry(
        canonical_root=str(root.resolve()),
        canonical_path=str(file.resolve()),
        display_path=file.name,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        folder_source="host_api",
    )


@pytest.fixture
async def replacement_db(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(Job.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    nas = tmp_path / "nas"
    nas.mkdir()
    source_file = nas / "handbook.pdf"
    source_file.write_bytes(b"old NAS")
    private = tmp_path / "uploads" / "source" / "document_handbook.pdf"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"old private")
    async with sessions() as session:
        source = Source(
            id="source",
            name="Private",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="config",
        )
        document = Document(
            id="document",
            source_id=source.id,
            filename="handbook.pdf",
            content_type="application/pdf",
            size_bytes=len(b"old private"),
            storage_path=str(private),
            status=DocumentStatus.READY,
            sag_source_id="derived-old",
            origin_kind="fnos_shared",
            origin_key=nas_origin_key(nas.resolve(), source_file.resolve()),
            origin_path=str(source_file.resolve()),
            origin_display_path="handbook.pdf",
            origin_size_bytes=len(b"old NAS"),
            origin_mtime_ns=source_file.stat().st_mtime_ns,
            origin_sha256=hashlib.sha256(b"old NAS").hexdigest(),
        )
        batch = Job(
            id="batch",
            type=JobType.IMPORT_NAS_DOCUMENTS,
            status=JobStatus.RUNNING,
            source_id=source.id,
            payload={"owner_uid": 1000},
        )
        session.add_all([source, document, batch])
        await session.commit()
        yield session, source, document, batch, nas, source_file, private
    await engine.dispose()


@pytest.mark.asyncio
async def test_unchanged_digest_only_refreshes_metadata(replacement_db, tmp_path: Path) -> None:
    session, _source, document, batch, nas, source_file, private = replacement_db
    source_file.write_bytes(b"old NAS")
    queue = Queue()
    outcome = await FnOSNasImporter(
        Access(), Host(), upload_dir=tmp_path / "uploads", job_queue=queue
    ).import_one(
        session,
        batch,
        _entry(nas, source_file),
        identity=GatewayIdentity(1000, "Alice", True),
    )

    assert outcome.outcome == "skipped"
    await session.refresh(document)
    assert document.origin_mtime_ns == source_file.stat().st_mtime_ns
    assert private.read_bytes() == b"old private"
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_changed_digest_stages_reprocess_and_keeps_old_bytes_until_handler(
    replacement_db, tmp_path: Path
) -> None:
    session, source, document, batch, nas, source_file, private = replacement_db
    source_file.write_bytes(b"new content")
    queue = Queue()
    outcome = await FnOSNasImporter(
        Access(), Host(), upload_dir=tmp_path / "uploads", job_queue=queue
    ).import_one(
        session,
        batch,
        _entry(nas, source_file),
        identity=GatewayIdentity(1000, "Alice", True),
    )

    assert outcome.outcome == "updated"
    replacement = await session.scalar(
        select(Job).where(Job.type == JobType.REPROCESS_DOCUMENT, Job.document_id == document.id)
    )
    assert replacement is not None
    assert replacement.payload["replacement"]["state"] == "staged"
    assert replacement.payload["derived_source_ids"] == ["derived-old"]
    assert Path(replacement.payload["replacement"]["staged_path"]).read_bytes() == b"new content"
    assert private.read_bytes() == b"old private"
    await session.refresh(document)
    assert document.status is DocumentStatus.PENDING
    assert queue.maintenance == [(source.id, replacement.id)]

    engine = Engine()
    await reprocess_document_task(
        session,
        replacement,
        engine_manager=engine,
        job_queue=queue,
    )
    await session.refresh(replacement)
    await session.refresh(document)
    assert private.read_bytes() == b"new content"
    assert replacement.payload["replacement"]["state"] == "installed"
    assert replacement.payload["cleanup_completed"] is True
    assert engine.deleted == ["derived-old"]
    assert document.origin_sha256 == hashlib.sha256(b"new content").hexdigest()
    child = await session.get(Job, replacement.payload["process_job_id"])
    assert child is not None and child.type is JobType.PROCESS_DOCUMENT


@pytest.mark.asyncio
async def test_changed_busy_document_keeps_old_bytes(replacement_db, tmp_path: Path) -> None:
    session, _source, document, batch, nas, source_file, private = replacement_db
    document.status = DocumentStatus.PAUSED
    await session.commit()
    source_file.write_bytes(b"new content")

    with pytest.raises(ConflictError) as captured:
        await FnOSNasImporter(
            Access(), Host(), upload_dir=tmp_path / "uploads", job_queue=Queue()
        ).import_one(
            session,
            batch,
            _entry(nas, source_file),
            identity=GatewayIdentity(1000, "Alice", True),
        )

    assert captured.value.code == "document_busy"
    assert private.read_bytes() == b"old private"
