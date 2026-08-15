from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.models import Document, Job, Source
from sag_api.enums import ConnectorKind, SourceType
from sag_api.jobs import JobQueue
from sag_api.services.document_service import StagedDocumentOrigin, register_document_from_staged_file


class FakeQueue(JobQueue):
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.ids.append(job_id)

    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass

    def source_maintenance_requested(self, source_id: str) -> bool:
        return False

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        pass


@pytest.mark.asyncio
async def test_register_staged_file_sanitizes_name_and_origin_is_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(Job.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    queue = FakeQueue()
    origin = StagedDocumentOrigin(
        kind="fnos_shared",
        key="a" * 64,
        path="/vol1/Documents/file.pdf",
        display_path="Documents/file.pdf",
        size_bytes=3,
        mtime_ns=1,
        sha256="b" * 64,
    )

    async with sessions() as session:
        source = Source(
            name="Private",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="config",
        )
        session.add(source)
        await session.commit()
        first_stage = tmp_path / "first.stage"
        first_stage.write_bytes(b"pdf")
        document, child = await register_document_from_staged_file(
            session,
            source,
            staged_path=first_stage,
            filename="../../unsafe.pdf",
            content_type="application/pdf",
            size_bytes=3,
            origin=origin,
            upload_dir=tmp_path / "uploads",
            job_queue=queue,
        )
        assert child is not None
        assert document.filename == "unsafe.pdf"
        assert Path(document.storage_path).name.endswith("_unsafe.pdf")
        assert Path(document.storage_path).read_bytes() == b"pdf"

        second_stage = tmp_path / "second.stage"
        second_stage.write_bytes(b"pdf")
        existing, duplicate_child = await register_document_from_staged_file(
            session,
            source,
            staged_path=second_stage,
            filename="unsafe.pdf",
            content_type="application/pdf",
            size_bytes=3,
            origin=origin,
            upload_dir=tmp_path / "uploads",
            job_queue=queue,
        )
        assert existing.id == document.id
        assert duplicate_child is None
        assert not second_stage.exists()
        assert queue.ids == [child.id]

    await engine.dispose()
