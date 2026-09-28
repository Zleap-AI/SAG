"""Native 0.7.1 knowledge is quarantined until its owner requests reingestion."""

import asyncio

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
    from sag_api.db.models import Document, Job, Setting, Source, UniverseOverview, UniversePartition, User
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
        user = User(email="legacy@test.invalid", password_hash="test", name="Legacy")
        session.add(user)
        await session.flush()
        overview = UniverseOverview(user_id=user.id, status="ready", is_active=True,
                                    schema_version=3, source_count=1, event_count=4, node_count=4)
        session.add(overview)
        await session.flush()
        partition = UniversePartition(overview_id=overview.id, user_id=user.id, source_id=source.id,
                                      kind="source", key=source.id, label="Book", x=0, y=0,
                                      event_count=4, node_count=4)
        session.add(partition)
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
        from sag_api.services.universe_service import universe_manifest

        manifest = await universe_manifest(session, user.id)
        assert manifest["status"] != "ready"
        assert manifest["counts"]["events"] == 0
        await session.refresh(overview)
        assert overview.is_active is False
        assert await session.get(UniversePartition, partition.id) is not None
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

        async def enqueue_durably(self, job_id: str) -> None:
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
@pytest.mark.parametrize("active_status", ["queued", "running"])
async def test_reingest_does_not_duplicate_an_active_retry(tmp_path, active_status):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.fnos.knowledge_upgrade import queue_legacy_reingest
    from sag_api.jobs.inproc import _mark_document_waiting_retry

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    original = uploads / "book.md"
    original.write_text("# Book", encoding="utf-8")

    class Queue:
        async def enqueue_durably(self, _job_id):
            raise AssertionError("an active retry must not create another job")

    async with sessions() as session:
        source = Source(name="Book", sag_source_config_id="book-source")
        session.add(source)
        await session.flush()
        document = Document(source_id=source.id, filename="book.md", storage_path=str(original),
                            status=DocumentStatus.FAILED, knowledge_state="failed")
        session.add(document)
        await session.flush()
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus(active_status),
                  source_id=source.id, document_id=document.id)
        session.add(job)
        await session.commit()
        assert await queue_legacy_reingest(session, uploads, Queue()) == {"queued": 0, "needs_file": 0}
        await _mark_document_waiting_retry(session, job)
        await session.commit()
        assert document.knowledge_state == "queued"
        assert await queue_legacy_reingest(session, uploads, Queue()) == {"queued": 0, "needs_file": 0}
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,required",
    [("pending", True), ("needs_file", True), ("queued", True), ("running", True), ("failed", True), ("ready", False)],
)
async def test_upgrade_status_requires_action_only_for_unfinished_knowledge(tmp_path, state, required):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Source
    from sag_api.fnos.knowledge_upgrade import reingest_status

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'status.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        source = Source(name="Legacy", sag_source_config_id="legacy")
        session.add(source)
        await session.flush()
        session.add(
            Document(
                source_id=source.id,
                filename="old.md",
                storage_path=str(tmp_path / "old.md"),
                knowledge_state=state,
            )
        )
        await session.commit()
        status = await reingest_status(session)
        assert status["required"] is required
        assert status["total"] == 1
        assert status["states"][state] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_workspace_records_engine_without_quarantining_documents(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Setting, Source
    from sag_api.enums import DocumentStatus
    from sag_api.fnos.knowledge_upgrade import mark_legacy_knowledge_pending

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        source = Source(name="Fresh", sag_source_config_id="fresh")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="fresh.md", storage_path=str(tmp_path / "fresh.md"),
            status=DocumentStatus.READY,
        )
        session.add(document)
        await session.commit()
        assert await mark_legacy_knowledge_pending(session, tmp_path / "missing-engine", tmp_path / "uploads") == 0
        assert await mark_legacy_knowledge_pending(session, tmp_path / "missing-engine", tmp_path / "uploads") == 0
        await session.refresh(document)
        assert document.status == DocumentStatus.READY
        assert document.knowledge_state is None
        marker = await session.scalar(select(Setting).where(Setting.key == "fnos_knowledge_engine_0_13"))
        assert marker.value == {"engine": "0.13.0", "legacy_retained": False}
    await engine.dispose()


@pytest.mark.asyncio
async def test_reingest_recovers_partial_dispatch_failure_without_duplicate_jobs(tmp_path, monkeypatch):
    from sag_api.db.base import Base
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus
    from sag_api.fnos.knowledge_upgrade import queue_legacy_reingest
    from sag_api.jobs.inproc import InProcessAsyncQueue

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dispatch.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    queue = InProcessAsyncQueue(sessions, engine_manager=None, concurrency=0)
    enqueue = queue.enqueue
    attempts = 0
    delivered = asyncio.Event()

    async def flaky_enqueue(job_id):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("dispatch temporarily unavailable")
        await enqueue(job_id)
        if queue._queue.qsize() == 3:
            delivered.set()

    monkeypatch.setattr(queue, "enqueue", flaky_enqueue)
    monkeypatch.setattr("sag_api.jobs.inproc._RETRY_ENQUEUE_RETRY_SECONDS", 0.0)
    try:
        async with sessions() as session:
            source = Source(name="Legacy", sag_source_config_id="legacy")
            session.add(source)
            await session.flush()
            for index in range(3):
                original = uploads / f"book-{index}.md"
                original.write_text("# Book", encoding="utf-8")
                session.add(Document(source_id=source.id, filename=original.name,
                                     storage_path=str(original), status=DocumentStatus.STALE,
                                     knowledge_state="pending"))
            await session.commit()
            assert await queue_legacy_reingest(session, uploads, queue) == {"queued": 3, "needs_file": 0}
            await asyncio.wait_for(delivered.wait(), timeout=1)
            assert await queue_legacy_reingest(session, uploads, queue) == {"queued": 0, "needs_file": 0}
            job_ids = set((await session.scalars(select(Job.id))).all())
            queued_ids = {queue._queue.get_nowait()[2] for _ in range(3)}
            assert queued_ids == job_ids
            assert len(job_ids) == 3
    finally:
        await queue.stop()
        await engine.dispose()
