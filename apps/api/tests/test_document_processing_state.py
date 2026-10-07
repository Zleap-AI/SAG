"""Processing counters are display facts, not durable extraction checkpoints."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from sag_api.core import db
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.control import JobPaused
from sag_api.jobs.inproc import _mark_document_waiting_retry
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome
from sag_api.schemas.document import DocumentOut
from sag_api.services.document_service import create_document_from_upload, resume_document


class Queue:
    async def enqueue_durably(self, _job_id):
        pass


@pytest.fixture
async def processing_document():
    await init_db()
    checkpoint = ProcessCheckpoint(source_id="article", chunk_ids=[str(i) for i in range(2033)], chunk_version="v1")
    async with SessionLocal() as session:
        source = Source(name="processing-state", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="stage.md", storage_path="/tmp/stage.md", status=DocumentStatus.PENDING
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            document_id=document.id,
            source_id=source.id,
            payload=checkpoint.merge_payload({"custom": "untouched"}),
        )
        session.add(job)
        await session.commit()
        ids = source.id, document.id, job.id
    yield (*ids, checkpoint)
    async with SessionLocal() as session:
        await session.execute(delete(Job).where(Job.id == ids[2]))
        await session.execute(delete(Document).where(Document.id == ids[1]))
        await session.execute(delete(Source).where(Source.id == ids[0]))
        await session.commit()


async def snapshot(document_id):
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        return SimpleNamespace(
            status=document.status,
            stage=getattr(document, "processing_stage", None),
            completed=getattr(document, "processed_chunks", None),
            total=getattr(document, "total_chunks", None),
            run_id=getattr(document, "processing_run_id", None),
            api=DocumentOut.model_validate(document).model_dump(),
        )


@pytest.mark.asyncio
async def test_upgrade_preserves_unknown_counts_and_is_repeatable(tmp_path, monkeypatch):
    legacy = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with legacy.begin() as connection:
            await connection.execute(text("CREATE TABLE documents (id TEXT PRIMARY KEY, progress INTEGER)"))
            await connection.execute(text("INSERT INTO documents VALUES ('old', 52)"))
        monkeypatch.setattr(db, "engine", legacy)
        await db._ensure_columns()
        await db._ensure_columns()
        async with legacy.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {c["name"] for c in inspect(sync).get_columns("documents")}
            )
            assert {"processing_stage", "processed_chunks", "total_chunks", "processing_run_id"} <= columns
            row = (
                await connection.execute(
                    text(
                        "SELECT progress, processing_stage, processed_chunks, total_chunks, processing_run_id "
                        "FROM documents"
                    )
                )
            ).one()
            assert tuple(row) == (52, None, None, None, None)
    finally:
        await legacy.dispose()


@pytest.mark.asyncio
async def test_upload_public_contract_starts_queued_without_inventing_counts(tmp_path):
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="queued-upload", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.commit()
        document, job = await create_document_from_upload(
            session,
            source,
            filename="queued.md",
            content_type="text/markdown",
            data=b"hello",
            upload_dir=str(tmp_path),
            job_queue=Queue(),
        )
        public = DocumentOut.model_validate(document).model_dump()
        assert public.get("processing_stage") == "queued"
        assert "processed_chunks" in public and public["processed_chunks"] is None
        assert "total_chunks" in public and public["total_chunks"] is None
        assert "processing_run_id" not in public
        await session.execute(delete(Job).where(Job.id == job.id))
        await session.execute(delete(Document).where(Document.id == document.id))
        await session.execute(delete(Source).where(Source.id == source.id))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "pause"])
async def test_counts_advance_within_same_percent_and_finalizing_is_not_success(ending, processing_document):
    _source_id, document_id, job_id, checkpoint = processing_document

    class Manager:
        async def process_document(self, *_args, on_stage, on_progress, on_checkpoint, **_kwargs):
            await on_stage("waiting_extraction")
            state = await snapshot(document_id)
            assert state.stage == "waiting_extraction"
            assert state.run_id
            await on_stage("extracting")
            assert (await snapshot(document_id)).completed == 0
            await on_progress(811, 2033)
            await on_progress(812, 2033)
            await on_progress(810, 2033)
            state = await snapshot(document_id)
            assert (state.stage, state.completed, state.total) == ("extracting", 812, 2033)
            await on_progress(2033, 2033)
            state = await snapshot(document_id)
            assert (state.status, state.stage, state.completed) == (DocumentStatus.EXTRACTING, "finalizing", 2033)
            async with SessionLocal() as read:
                assert (await read.get(Job, job_id)).payload == checkpoint.merge_payload({"custom": "untouched"})
            if ending == "failure":
                raise RuntimeError("batch rejected")
            if ending == "pause":
                return ProcessOutcome(paused=True)
            await on_checkpoint(checkpoint.model_copy(update={"processed_chunk_ids": checkpoint.chunk_ids}))
            assert (await snapshot(document_id)).stage == "finalizing"
            return ProcessOutcome(source_id="article", chunk_count=2033, processed_chunk_ids=checkpoint.chunk_ids)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if ending == "failure":
            with pytest.raises(RuntimeError, match="batch rejected"):
                await tasks._process_document_unlocked(session, job, engine_manager=Manager())
        elif ending == "pause":
            with pytest.raises(JobPaused):
                await tasks._process_document_unlocked(session, job, engine_manager=Manager())
        else:
            await tasks._process_document_unlocked(session, job, engine_manager=Manager())
    final = await snapshot(document_id)
    assert final.stage == ("ready" if ending == "success" else "finalizing")
    assert (
        final.status
        == {"success": DocumentStatus.READY, "failure": DocumentStatus.FAILED, "pause": DocumentStatus.PAUSED}[ending]
    )
    assert final.completed == final.total == 2033


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [DocumentStatus.PAUSING, DocumentStatus.DELETING])
async def test_stage_and_count_callbacks_preserve_control_snapshot(control, processing_document):
    _source_id, document_id, job_id, _checkpoint = processing_document
    observed = []

    class Manager:
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            await on_stage("extracting")
            await on_progress(811, 2033)
            async with SessionLocal() as control_session:
                await control_session.execute(update(Document).where(Document.id == document_id).values(status=control))
                await control_session.commit()
            await on_stage("finalizing")
            await on_progress(2033, 2033)
            state = await snapshot(document_id)
            observed.append((state.status, state.stage, state.completed))
            return ProcessOutcome(paused=True)

    async with SessionLocal() as session:
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(session, await session.get(Job, job_id), engine_manager=Manager())
    assert observed == [(control, "extracting", 811)]


@pytest.mark.asyncio
async def test_resume_invalidates_old_callbacks_and_resets_new_run(processing_document):
    source_id, document_id, job_id, _checkpoint = processing_document
    callbacks = {}

    class First:
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            callbacks.update(stage=on_stage, progress=on_progress)
            await on_stage("extracting")
            await on_progress(2033, 2033)
            return ProcessOutcome(paused=True)

    async with SessionLocal() as session:
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(session, await session.get(Job, job_id), engine_manager=First())
        job = await session.get(Job, job_id)
        job.status = JobStatus.PAUSED
        await session.commit()
        old_run = (await snapshot(document_id)).run_id
    async with SessionLocal() as session:
        await resume_document(session, await session.get(Source, source_id), document_id, job_queue=Queue())
        queued = await snapshot(document_id)
        assert (queued.stage, queued.run_id) == ("queued", None)
        await callbacks["stage"]("extracting")
        await callbacks["progress"](2033, 2033)
        assert (await snapshot(document_id)).stage == "queued"

    class Second:
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            await on_stage("extracting")
            state = await snapshot(document_id)
            assert state.run_id and state.run_id != old_run
            assert (state.completed, state.total) == (0, 2033)
            await callbacks["stage"]("finalizing")
            await callbacks["progress"](2033, 2033)
            assert (await snapshot(document_id)).completed == 0
            await on_progress(10, 2033)
            return ProcessOutcome(paused=True)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.RUNNING
        await session.commit()
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(session, job, engine_manager=Second())
    assert (await snapshot(document_id)).completed == 10


@pytest.mark.asyncio
async def test_retry_queue_invalidates_run_without_discarding_snapshot(processing_document):
    _source_id, document_id, job_id, _checkpoint = processing_document

    class Manager:
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            await on_stage("extracting")
            await on_progress(20, 2033)
            raise RuntimeError("transient failure")

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        with pytest.raises(RuntimeError, match="transient failure"):
            await tasks._process_document_unlocked(session, job, engine_manager=Manager())
        await _mark_document_waiting_retry(session, job)
        await session.commit()
    state = await snapshot(document_id)
    assert (state.stage, state.run_id, state.completed, state.total) == ("waiting_retry", None, 20, 2033)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["recover", "yield"])
async def test_requeued_worker_does_not_keep_showing_finalizing(transition, processing_document, monkeypatch):
    from sag_api.jobs.control import JobYielded
    from sag_api.jobs.inproc import InProcessAsyncQueue

    _source_id, document_id, job_id, _checkpoint = processing_document
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.EXTRACTING
        document.processing_stage = "finalizing"
        document.processing_run_id = "old-execution"
        document.processed_chunks = document.total_chunks = 2033
        job = await session.get(Job, job_id)
        job.status = JobStatus.RUNNING if transition == "recover" else JobStatus.QUEUED
        await session.commit()
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None)
    if transition == "recover":
        await queue._recover()
    else:

        async def yield_handler(*_args, **_kwargs):
            raise JobYielded("source_maintenance")

        monkeypatch.setitem(tasks.TASK_HANDLERS, JobType.PROCESS_DOCUMENT, yield_handler)
        await queue._run_job(job_id)
    state = await snapshot(document_id)
    assert (state.stage, state.run_id, state.completed) == ("queued", None, 2033)


@pytest.mark.asyncio
async def test_real_display_transaction_failure_does_not_poison_checkpoint_session(processing_document, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    _source_id, document_id, job_id, checkpoint = processing_document

    class FailingSession(AsyncSession):
        async def execute(self, *_args, **_kwargs):
            return await super().execute(text("INSERT INTO missing_progress_table VALUES (1)"))

    failing_factory = async_sessionmaker(db.engine, class_=FailingSession)

    class Manager:
        async def process_document(self, *_args, on_stage, on_progress, on_checkpoint, **_kwargs):
            await on_stage("extracting")
            with monkeypatch.context() as patch:
                patch.setattr(tasks, "SessionLocal", failing_factory)
                await on_stage("finalizing")
                await on_progress(2033, 2033)
            await on_checkpoint(checkpoint.model_copy(update={"processed_chunk_ids": checkpoint.chunk_ids}))
            return ProcessOutcome(source_id="article", chunk_count=2033, processed_chunk_ids=checkpoint.chunk_ids)

    async with SessionLocal() as session:
        await tasks._process_document_unlocked(session, await session.get(Job, job_id), engine_manager=Manager())
    state = await snapshot(document_id)
    assert state.status == DocumentStatus.READY
    assert (state.stage, state.completed, state.total) == ("ready", 2033, 2033)
    async with SessionLocal() as session:
        persisted = ProcessCheckpoint.from_payload((await session.get(Job, job_id)).payload)
        assert persisted.processed_chunk_ids == checkpoint.chunk_ids


@pytest.mark.asyncio
async def test_preparation_stages_keep_unknown_counts_until_ingest_commits(processing_document, monkeypatch):
    from sag_api.parsing.service import PreparedDocument

    _source_id, document_id, job_id, checkpoint = processing_document
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.payload = {}
        await session.commit()

    async def prepare(_path, _settings, *, on_state, **_kwargs):
        await on_state({"provider": "anydoc", "status": "running"})
        current = await snapshot(document_id)
        assert (current.stage, current.completed, current.total) == ("parsing", None, None)
        return PreparedDocument(path="/tmp/prepared.md", provider="anydoc")

    class Manager:
        async def process_document(self, *_args, on_stage, on_checkpoint, **_kwargs):
            for stage in ["parsing", "chunking", "indexing"]:
                await on_stage(stage)
                current = await snapshot(document_id)
                assert (current.stage, current.completed, current.total) == (stage, None, None)
            await on_checkpoint(checkpoint)
            current = await snapshot(document_id)
            assert (current.stage, current.completed, current.total) == ("indexing", 0, 2033)
            await on_stage("waiting_extraction")
            await on_stage("extracting")
            await on_stage("finalizing")
            return ProcessOutcome(source_id="article", chunk_count=2033, processed_chunk_ids=checkpoint.chunk_ids)

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    async with SessionLocal() as session:
        await tasks._process_document_unlocked(session, await session.get(Job, job_id), engine_manager=Manager())
    assert (await snapshot(document_id)).stage == "ready"
