"""Processing counters are display facts, not durable extraction checkpoints."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from document_requests import document_receipt, document_request
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from zleap.sag.operations import ProcessSourceRequest

from sag_api.core import db
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.control import JobPaused
from sag_api.jobs.inproc import _mark_document_waiting_retry
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.schemas.document import DocumentOut
from sag_api.services.document_service import create_document_from_upload, resume_document
from tests.document_requests import UncommittedDocumentManager, controller_processing_identity, processing_parameters

pytestmark = pytest.mark.usefixtures("controller_identity")


class Queue:
    async def enqueue_durably(self, _job_id):
        pass


@pytest.fixture
async def processing_document():
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="processing-state", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        request = document_request(source.sag_source_config_id)
        document = Document(
            source_id=source.id,
            filename="stage.md",
            storage_path="/tmp/stage.md",
            status=DocumentStatus.PENDING,
            processed_chunks=0,
            total_chunks=2033,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            document_id=document.id,
            source_id=source.id,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": request.model_dump(mode="json"),
                "control_revision": 1,
                "custom": "untouched",
            },
        )
        session.add(job)
        await session.commit()
        ids = source.id, document.id, job.id
    yield (*ids, request)
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
    _source_id, document_id, job_id, request = processing_document

    class Manager(UncommittedDocumentManager):
        async def process_document(self, _source_id, request_factory, *, on_stage, on_progress, **_kwargs):
            assert await request_factory(None, *processing_parameters()) == request
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
                assert (await read.get(Job, job_id)).payload == {
                    "processing_identity": controller_processing_identity("/tmp/stage.md"),
                    "process_request": request.model_dump(mode="json"),
                    "control_revision": 1,
                    "custom": "untouched",
                }
            if ending == "failure":
                raise RuntimeError("batch rejected")
            if ending == "pause":
                return None
            assert (await snapshot(document_id)).stage == "finalizing"
            return document_receipt(request, chunk_count=2033)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if ending == "failure":
            with pytest.raises(RuntimeError, match="batch rejected"):
                await tasks._process_document_unlocked(
                    session, job, engine_manager=Manager(), lease_lost=asyncio.Event()
                )
        elif ending == "pause":
            with pytest.raises(JobPaused):
                await tasks._process_document_unlocked(
                    session, job, engine_manager=Manager(), lease_lost=asyncio.Event()
                )
        else:
            await tasks._process_document_unlocked(session, job, engine_manager=Manager(), lease_lost=asyncio.Event())
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
    _source_id, document_id, job_id, _request = processing_document
    observed = []

    class Manager(UncommittedDocumentManager):
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
            return None

    async with SessionLocal() as session:
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(
                session, await session.get(Job, job_id), engine_manager=Manager(), lease_lost=asyncio.Event()
            )
    assert observed == [(control, "extracting", 811)]


@pytest.mark.asyncio
async def test_resume_invalidates_old_callbacks_and_resets_new_run(processing_document, document_engine_manager):
    source_id, document_id, job_id, _request = processing_document
    callbacks = {}

    class First(UncommittedDocumentManager):
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            callbacks.update(stage=on_stage, progress=on_progress)
            await on_stage("extracting")
            await on_progress(2033, 2033)
            return None

    async with SessionLocal() as session:
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(
                session, await session.get(Job, job_id), engine_manager=First(), lease_lost=asyncio.Event()
            )
        job = await session.get(Job, job_id)
        job.status = JobStatus.PAUSED
        await session.commit()
        old_run = (await snapshot(document_id)).run_id
    async with SessionLocal() as session:
        await resume_document(
            session,
            await session.get(Source, source_id),
            document_id,
            job_queue=Queue(),
            engine_manager=document_engine_manager,
        )
        queued = await snapshot(document_id)
        assert (queued.stage, queued.run_id) == ("queued", None)
        await callbacks["stage"]("extracting")
        await callbacks["progress"](2033, 2033)
        assert (await snapshot(document_id)).stage == "queued"

    class Second(UncommittedDocumentManager):
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            await on_stage("extracting")
            state = await snapshot(document_id)
            assert state.run_id and state.run_id != old_run
            assert (state.completed, state.total) == (0, 2033)
            await callbacks["stage"]("finalizing")
            await callbacks["progress"](2033, 2033)
            assert (await snapshot(document_id)).completed == 0
            await on_progress(10, 2033)
            return None

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.RUNNING
        await session.commit()
        with pytest.raises(JobPaused):
            await tasks._process_document_unlocked(session, job, engine_manager=Second(), lease_lost=asyncio.Event())
    assert (await snapshot(document_id)).completed == 10


@pytest.mark.asyncio
async def test_retry_queue_invalidates_run_without_discarding_snapshot(processing_document):
    _source_id, document_id, job_id, _request = processing_document

    class Manager(UncommittedDocumentManager):
        async def process_document(self, *_args, on_stage, on_progress, **_kwargs):
            await on_stage("extracting")
            await on_progress(20, 2033)
            raise RuntimeError("transient failure")

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        with pytest.raises(RuntimeError, match="transient failure"):
            await tasks._process_document_unlocked(session, job, engine_manager=Manager(), lease_lost=asyncio.Event())
        await _mark_document_waiting_retry(session, job)
        await session.commit()
    state = await snapshot(document_id)
    assert (state.stage, state.run_id, state.completed, state.total) == ("waiting_retry", None, 20, 2033)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["recover", "yield"])
async def test_requeued_worker_does_not_keep_showing_finalizing(transition, processing_document, monkeypatch):
    from sag_api.jobs.control import JobYielded
    from sag_api.jobs.inproc import InProcessAsyncQueue

    _source_id, document_id, job_id, _request = processing_document
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
    expected_stage = "waiting_manual" if transition == "recover" else "queued"
    assert (state.stage, state.run_id, state.completed) == (expected_stage, None, 2033)


@pytest.mark.asyncio
async def test_real_display_transaction_failure_does_not_poison_request_session(processing_document, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    _source_id, document_id, job_id, request = processing_document

    class FailingSession(AsyncSession):
        async def execute(self, *_args, **_kwargs):
            return await super().execute(text("INSERT INTO missing_progress_table VALUES (1)"))

    failing_factory = async_sessionmaker(db.engine, class_=FailingSession)

    class Manager(UncommittedDocumentManager):
        async def process_document(self, _source_id, request_factory, *, on_stage, on_progress, **_kwargs):
            assert await request_factory(None, *processing_parameters()) == request
            await on_stage("extracting")
            with monkeypatch.context() as patch:
                patch.setattr(tasks, "SessionLocal", failing_factory)
                await on_stage("finalizing")
                await on_progress(2033, 2033)
            return document_receipt(request, chunk_count=2033)

    async with SessionLocal() as session:
        await tasks._process_document_unlocked(
            session, await session.get(Job, job_id), engine_manager=Manager(), lease_lost=asyncio.Event()
        )
    state = await snapshot(document_id)
    assert state.status == DocumentStatus.READY
    assert (state.stage, state.completed, state.total) == ("ready", 2033, 2033)
    async with SessionLocal() as session:
        persisted = ProcessSourceRequest.model_validate((await session.get(Job, job_id)).payload["process_request"])
        assert persisted == request


@pytest.mark.asyncio
async def test_preparation_stages_keep_unknown_counts_until_public_extraction_starts(
    processing_document, monkeypatch, tmp_path
):
    from sag_api.parsing.service import PreparedDocument

    _source_id, document_id, job_id, request = processing_document
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.payload = {}
        await session.commit()

    markdown = tmp_path / "prepared.md"
    markdown.write_text("# A\nA documented fact.")
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        document.storage_path = str(markdown)
        await session.commit()

    async def prepare(_path, _settings, *, on_state, **_kwargs):
        await on_state({"provider": "anydoc", "status": "running"})
        current = await snapshot(document_id)
        assert (current.stage, current.completed, current.total) == ("parsing", None, None)
        return PreparedDocument(path=str(markdown), provider="anydoc")

    class Manager(UncommittedDocumentManager):
        async def process_document(self, source_config_id, request_factory, *, on_stage, on_progress, **_kwargs):
            processor = IncrementalDocumentProcessor(
                SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language="en"))),
                source_config_id,
                max_concurrency=1,
            )
            bound = await request_factory(processor, *processing_parameters())
            for stage in ["parsing", "chunking", "indexing"]:
                await on_stage(stage)
                current = await snapshot(document_id)
                assert (current.stage, current.completed, current.total) == (stage, None, None)
            await on_stage("extracting")
            await on_progress(0, 2033)
            current = await snapshot(document_id)
            assert (current.stage, current.completed, current.total) == ("extracting", 0, 2033)
            await on_stage("finalizing")
            return document_receipt(bound, chunk_count=2033)

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    async with SessionLocal() as session:
        await tasks._process_document_unlocked(
            session, await session.get(Job, job_id), engine_manager=Manager(), lease_lost=asyncio.Event()
        )
    assert (await snapshot(document_id)).stage == "ready"
