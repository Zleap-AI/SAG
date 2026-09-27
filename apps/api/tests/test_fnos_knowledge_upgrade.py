"""Native 0.7.1 knowledge is quarantined until its owner requests reingestion."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def test_old_internal_citations_are_marked_stale_without_changing_external_links():
    from sag_api.fnos.knowledge_upgrade import stale_internal_citations

    original = [{"n": 1, "chunk_id": "old"}, {"n": 2, "kind": "external", "url": "https://example.com"}]
    updated = stale_internal_citations(original)
    assert updated[0]["stale"] is True
    assert "stale" not in updated[1]
    assert "stale" not in original[0]


@pytest.mark.asyncio
async def test_legacy_detection_preserves_metadata_and_never_queues_model_work(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Job, Setting, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.fnos.knowledge_upgrade import mark_legacy_knowledge_pending

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    old_engine = tmp_path / "engine"
    old_engine.mkdir()
    (old_engine / "legacy.marker").write_text("0.7.1", encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    original = uploads / "book.md"
    original.write_text("# Book", encoding="utf-8")

    async with sessions() as session:
        source = Source(name="Book", sag_source_config_id="book-source", chunk_count=5, event_count=4)
        session.add(source)
        await session.flush()
        kept = Document(source_id=source.id, filename="book.md", storage_path=str(original),
                        status=DocumentStatus.READY, chunk_count=5, event_count=4)
        missing = Document(source_id=source.id, filename="missing.md", storage_path=str(uploads / "missing.md"),
                           status=DocumentStatus.READY)
        session.add_all([kept, missing])
        await session.flush()
        old_job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED,
                      source_id=source.id, document_id=kept.id)
        session.add_all([old_job, Setting(scope="global", key="model_config", value={"llm_model": "saved"})])
        await session.commit()

        assert await mark_legacy_knowledge_pending(session, old_engine, uploads) == 2
        assert await mark_legacy_knowledge_pending(session, old_engine, uploads) == 0
        await session.refresh(kept)
        await session.refresh(missing)
        await session.refresh(old_job)
        await session.refresh(source)
        assert kept.status == DocumentStatus.STALE
        assert kept.knowledge_state == "pending"
        assert missing.knowledge_state == "needs_file"
        assert old_job.status == JobStatus.PAUSED
        assert source.name == "Book" and source.chunk_count == 0
        saved_config = await session.scalar(select(Setting).where(Setting.key == "model_config"))
        assert saved_config.value == {"llm_model": "saved"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_legacy_engine_directory_still_marks_old_documents(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.fnos.knowledge_upgrade import mark_legacy_knowledge_pending

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    old_engine = tmp_path / "engine"
    old_engine.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    async with sessions() as session:
        source = Source(name="Empty", sag_source_config_id="empty-source")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="missing.md",
            storage_path=str(uploads / "missing.md"),
            status=DocumentStatus.READY,
        )
        session.add(document)
        await session.commit()
        assert await mark_legacy_knowledge_pending(session, old_engine, uploads) == 1
        await session.refresh(document)
        assert document.knowledge_state == "needs_file"
    await engine.dispose()


@pytest.mark.asyncio
async def test_reingest_only_queues_private_originals_once(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.fnos.knowledge_upgrade import queue_legacy_reingest

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    original = uploads / "book.md"
    original.write_text("# Book", encoding="utf-8")

    class Queue:
        ids: list[str] = []

        async def enqueue(self, job_id: str) -> None:
            self.ids.append(job_id)

    queue = Queue()
    async with sessions() as session:
        source = Source(name="Book", sag_source_config_id="book-source")
        session.add(source)
        await session.flush()
        documents = [
            Document(source_id=source.id, filename="book.md", storage_path=str(original),
                     status=DocumentStatus.STALE, knowledge_state="pending"),
            Document(source_id=source.id, filename="gone.md", storage_path=str(uploads / "gone.md"),
                     status=DocumentStatus.STALE, knowledge_state="needs_file"),
        ]
        session.add_all(documents)
        await session.commit()

        first = await queue_legacy_reingest(session, uploads, queue)
        second = await queue_legacy_reingest(session, uploads, queue)
        assert first == {"queued": 1, "needs_file": 1}
        assert second == {"queued": 0, "needs_file": 1}
        assert len(queue.ids) == 1
        assert (await session.scalar(select(Job).where(Job.id == queue.ids[0]))).status == JobStatus.QUEUED
        await session.refresh(documents[0])
        assert documents[0].knowledge_state == "queued"
        assert original.read_text(encoding="utf-8") == "# Book"
    await engine.dispose()


@pytest.mark.asyncio
async def test_replacement_rejects_symlinked_private_upload_directory(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Source
    from sag_api.enums import DocumentStatus
    from sag_api.fnos.knowledge_upgrade import replace_missing_original

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replacement.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    async with sessions() as session:
        source = Source(name="Book", sag_source_config_id="book-source")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="book.md",
            storage_path=str(uploads / "book.md"),
            status=DocumentStatus.STALE,
            knowledge_state="needs_file",
        )
        session.add(document)
        await session.commit()
        (uploads / source.id).symlink_to(escaped, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            await replace_missing_original(
                session,
                document.id,
                filename="book.md",
                content_type="text/markdown",
                data=b"# Book",
                uploads_dir=uploads,
            )
        assert list(escaped.iterdir()) == []
    await engine.dispose()
