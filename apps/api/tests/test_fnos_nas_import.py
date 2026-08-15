from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.errors import ConflictError, ValidationError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import ConnectorKind, JobStatus, JobType, SourceType
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.nas_registry import NasScanEntry
from sag_api.fnos.open_api import UserACL
from sag_api.jobs import JobQueue
from sag_api.services.fnos_nas_import import COPY_CHUNK_SIZE, FnOSNasImporter


class FakeQueue(JobQueue):
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)

    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass

    def source_maintenance_requested(self, source_id: str) -> bool:
        return False

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass


class FakeAccess:
    def __init__(self) -> None:
        self.revoked = False

    async def revalidate_root(self, _session, _identity, _root) -> None:
        if self.revoked:
            raise ConflictError("revoked", code="nas_folder_revoked")


class FakeHost:
    def __init__(self) -> None:
        self.readable = True
        self.paths: list[str] = []

    async def check_user_acl(self, uid: int, paths: list[str]) -> list[UserACL]:
        assert uid == 1000
        self.paths.extend(paths)
        return [UserACL(path=path, readable=self.readable, writable=False, deletable=False) for path in paths]


@pytest.fixture
async def import_db() -> tuple[AsyncSession, Source, Job]:
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
            payload={"owner_uid": 1000},
        )
        session.add(job)
        await session.commit()
        yield session, source, job
    await engine.dispose()


def _entry(root: Path, file: Path) -> NasScanEntry:
    metadata = file.stat()
    return NasScanEntry(
        canonical_root=str(root.resolve()),
        canonical_path=str(file.resolve()),
        display_path=f"Policies/{file.name}",
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        folder_source="host_api",
    )


@pytest.mark.asyncio
async def test_importer_streams_to_private_storage_and_creates_child_job(
    tmp_path: Path, import_db: tuple[AsyncSession, Source, Job]
) -> None:
    session, source, job = import_db
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    content = b"a" * (COPY_CHUNK_SIZE + 37)
    source_file = nas_root / "handbook.pdf"
    source_file.write_bytes(content)
    uploads = tmp_path / "private-uploads"
    queue = FakeQueue()
    host = FakeHost()
    importer = FnOSNasImporter(
        FakeAccess(),
        host,
        upload_dir=uploads,
        job_queue=queue,
    )

    outcome = await importer.import_one(
        session,
        job,
        _entry(nas_root, source_file),
        identity=GatewayIdentity(1000, "Alice", True),
    )

    assert outcome.outcome == "created"
    document = await session.get(Document, outcome.document_id)
    assert document is not None
    private_path = Path(document.storage_path)
    assert private_path.is_relative_to(uploads)
    assert private_path != source_file
    assert private_path.read_bytes() == content
    assert document.origin_path == str(source_file.resolve())
    assert document.origin_display_path == "Policies/handbook.pdf"
    assert document.origin_size_bytes == len(content)
    assert document.origin_sha256 == hashlib.sha256(content).hexdigest()
    child = await session.scalar(
        select(Job).where(Job.type == JobType.PROCESS_DOCUMENT, Job.document_id == document.id)
    )
    assert child is not None
    assert queue.enqueued == [child.id]
    assert host.paths == [str(source_file.resolve())]
    assert not (uploads / ".nas-stage" / job.id).exists()


@pytest.mark.asyncio
async def test_importer_rejects_changed_file_and_cleans_stage(
    tmp_path: Path, import_db: tuple[AsyncSession, Source, Job]
) -> None:
    session, _source, job = import_db
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    source_file = nas_root / "handbook.pdf"
    source_file.write_bytes(b"original")
    entry = _entry(nas_root, source_file)
    source_file.write_bytes(b"changed after scan")
    uploads = tmp_path / "uploads"

    with pytest.raises(ConflictError) as captured:
        await FnOSNasImporter(
            FakeAccess(), FakeHost(), upload_dir=uploads, job_queue=FakeQueue()
        ).import_one(
            session,
            job,
            entry,
            identity=GatewayIdentity(1000, "Alice", True),
        )

    assert captured.value.code == "nas_file_changed"
    assert not (uploads / ".nas-stage" / job.id).exists()


@pytest.mark.asyncio
async def test_importer_rejects_revoked_acl_and_symlinks(
    tmp_path: Path, import_db: tuple[AsyncSession, Source, Job]
) -> None:
    session, _source, job = import_db
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    real = nas_root / "real.pdf"
    real.write_bytes(b"pdf")
    link = nas_root / "link.pdf"
    link.symlink_to(real)
    uploads = tmp_path / "uploads"
    host = FakeHost()
    host.readable = False
    importer = FnOSNasImporter(FakeAccess(), host, upload_dir=uploads, job_queue=FakeQueue())

    with pytest.raises(ConflictError) as acl_error:
        await importer.import_one(
            session,
            job,
            _entry(nas_root, real),
            identity=GatewayIdentity(1000, "Alice", True),
        )
    assert acl_error.value.code == "nas_file_unreadable"

    metadata = real.stat()
    symlink_entry = NasScanEntry(
        canonical_root=str(nas_root.resolve()),
        canonical_path=str(link),
        display_path="link.pdf",
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        folder_source="legacy_manual",
    )
    with pytest.raises(ValidationError, match="安全"):
        await importer.import_one(
            session,
            job,
            symlink_entry,
            identity=GatewayIdentity(1000, "Alice", True),
        )


@pytest.mark.asyncio
async def test_importer_stops_before_copy_when_root_is_revoked(
    tmp_path: Path, import_db: tuple[AsyncSession, Source, Job]
) -> None:
    session, _source, job = import_db
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    file = nas_root / "file.pdf"
    file.write_bytes(b"pdf")
    access = FakeAccess()
    access.revoked = True
    uploads = tmp_path / "uploads"

    with pytest.raises(ConflictError):
        await FnOSNasImporter(
            access, FakeHost(), upload_dir=uploads, job_queue=FakeQueue()
        ).import_one(
            session,
            job,
            _entry(nas_root, file),
            identity=GatewayIdentity(1000, "Alice", True),
        )
    assert not uploads.exists()
