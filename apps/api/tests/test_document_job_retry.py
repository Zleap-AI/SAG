from types import SimpleNamespace
from uuid import uuid4

import pytest

from sag_api.enums import DocumentStatus
from sag_api.jobs.inproc import _mark_document_waiting_retry


@pytest.mark.asyncio
async def test_retry_marks_document_pending_without_resetting_checkpoint_metrics():
    document = SimpleNamespace(
        status=DocumentStatus.FAILED,
        error="upstream timeout",
        progress=64,
        chunk_count=12,
        event_count=7,
        token_usage=9_000,
        sag_source_id="derived-source",
    )

    class FakeSession:
        async def get(self, _model, document_id):
            assert document_id == "document-1"
            return document

    job = SimpleNamespace(document_id="document-1")

    await _mark_document_waiting_retry(FakeSession(), job)

    assert document.status == DocumentStatus.PENDING
    assert document.error is None
    assert document.progress == 64
    assert document.chunk_count == 12
    assert document.event_count == 7
    assert document.token_usage == 9_000
    assert document.sag_source_id == "derived-source"


@pytest.mark.asyncio
async def test_non_document_retry_does_not_query_for_a_document():
    class FakeSession:
        async def get(self, _model, _document_id):
            raise AssertionError("a non-document job must not load a document")

    await _mark_document_waiting_retry(FakeSession(), SimpleNamespace(document_id=None))


@pytest.mark.asyncio
async def test_worker_commits_retryable_job_and_document_as_waiting(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="retry-source",
            description="",
            sag_source_config_id=f"retry-source-config-{uuid4().hex}",
            config={},
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="retry.md",
            content_type="text/markdown",
            size_bytes=128,
            storage_path="/tmp/retry.md",
            status=DocumentStatus.EXTRACTING,
            progress=64,
            chunk_count=12,
            event_count=7,
            token_usage=9_000,
            sag_source_id="derived-source",
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id

    async def retryable_handler(session, job, **_kwargs):
        document = await session.get(Document, job.document_id)
        document.status = DocumentStatus.FAILED
        document.error = "upstream timeout"
        await session.commit()
        raise ServiceUnavailableError("upstream timeout")

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, retryable_handler)
    scheduled: list[str] = []
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    monkeypatch.setattr(
        queue,
        "_schedule_retry",
        lambda queued_job_id, _delay: scheduled.append(queued_job_id),
    )

    await queue._run_job(job_id)

    async with SessionLocal() as session:
        waiting_job = await session.get(Job, job_id)
        waiting_document = await session.get(Document, document_id)
        assert waiting_job.status == JobStatus.QUEUED
        assert waiting_document.status == DocumentStatus.PENDING
        assert waiting_document.error is None
        assert waiting_document.progress == 64
        assert waiting_document.chunk_count == 12
        assert waiting_document.event_count == 7
        assert waiting_document.token_usage == 9_000
        assert waiting_document.sag_source_id == "derived-source"
        assert scheduled == [job_id]
        await session.delete(await session.get(Source, source_id))
        await session.commit()


@pytest.mark.asyncio
async def test_worker_keeps_non_retryable_document_failed(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ValidationError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="final-failure-source",
            description="",
            sag_source_config_id=f"final-failure-source-config-{uuid4().hex}",
            config={},
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="invalid.md",
            content_type="text/markdown",
            size_bytes=64,
            storage_path="/tmp/invalid.md",
            status=DocumentStatus.EXTRACTING,
            progress=20,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id

    async def invalid_handler(session, job, **_kwargs):
        document = await session.get(Document, job.document_id)
        document.status = DocumentStatus.FAILED
        document.error = "invalid document"
        await session.commit()
        raise ValidationError("invalid document")

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, invalid_handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)

    await queue._run_job(job_id)

    async with SessionLocal() as session:
        failed_job = await session.get(Job, job_id)
        failed_document = await session.get(Document, document_id)
        assert failed_job.status == JobStatus.FAILED
        assert failed_job.error == "invalid document"
        assert failed_document.status == DocumentStatus.FAILED
        assert failed_document.error == "invalid document"
        await session.delete(await session.get(Source, source_id))
        await session.commit()
