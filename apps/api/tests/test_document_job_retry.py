import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sag_api.enums import DocumentStatus, JobType
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

    job = SimpleNamespace(document_id="document-1", type=JobType.PROCESS_DOCUMENT)

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

    await _mark_document_waiting_retry(
        FakeSession(),
        SimpleNamespace(document_id=None, type=JobType.INDEX_UNIVERSE),
    )


@pytest.mark.asyncio
async def test_retryable_handler_failure_cannot_revive_concurrently_paused_job(
    monkeypatch,
):
    from sqlalchemy import update

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-wins-retry-race",
            sag_source_config_id=f"pause-wins-retry-race-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause.md",
            status=DocumentStatus.EXTRACTING,
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
        document_id, job_id = document.id, job.id

    entered = asyncio.Event()
    fail_now = asyncio.Event()

    async def retryable_handler(*_args, **_kwargs):
        entered.set()
        await fail_now.wait()
        raise ServiceUnavailableError("transient extraction failure")

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, retryable_handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    scheduled = []
    monkeypatch.setattr(
        queue,
        "_schedule_retry",
        lambda queued_job_id, _delay: scheduled.append(queued_job_id),
    )
    running = asyncio.create_task(queue._run_job(job_id))
    await asyncio.wait_for(entered.wait(), timeout=1)

    async with SessionLocal() as session:
        await session.execute(
            update(Job).where(Job.id == job_id).values(status=JobStatus.PAUSED)
        )
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.PAUSING)
        )
        await session.commit()

    fail_now.set()
    await asyncio.wait_for(running, timeout=1)

    async with SessionLocal() as session:
        paused_job = await session.get(Job, job_id)
        paused_document = await session.get(Document, document_id)
        assert paused_job.status == JobStatus.PAUSED
        assert paused_document.status == DocumentStatus.PAUSED
    assert scheduled == []


@pytest.mark.asyncio
async def test_job_paused_signal_cannot_undo_a_concurrent_resume(monkeypatch):
    from sqlalchemy import update

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-wins-stale-pause-signal",
            sag_source_config_id=f"resume-wins-stale-pause-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume.md",
            status=DocumentStatus.EXTRACTING,
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
        job_id = job.id

    entered = asyncio.Event()
    pause_now = asyncio.Event()

    async def stale_pause_handler(*_args, **_kwargs):
        entered.set()
        await pause_now.wait()
        raise JobPaused

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, stale_pause_handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    running = asyncio.create_task(queue._run_job(job_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    resumed_payload = {"resume_requested": True, "checkpoint_version": 7}
    async with SessionLocal() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.QUEUED,
                started_at=None,
                payload=resumed_payload,
            )
        )
        await session.commit()

    pause_now.set()
    await asyncio.wait_for(running, timeout=1)

    async with SessionLocal() as session:
        resumed = await session.get(Job, job_id)
        assert resumed.status == JobStatus.QUEUED
        assert resumed.payload == resumed_payload


@pytest.mark.asyncio
async def test_job_yielded_signal_cannot_undo_a_concurrent_pause(monkeypatch):
    from sqlalchemy import update

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus
    from sag_api.jobs.control import JobYielded
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-wins-stale-yield",
            sag_source_config_id=f"pause-wins-stale-yield-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="yield.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/yield.md",
            status=DocumentStatus.EXTRACTING,
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
        document_id, job_id = document.id, job.id

    entered = asyncio.Event()
    yield_now = asyncio.Event()

    async def stale_yield_handler(*_args, **_kwargs):
        entered.set()
        await yield_now.wait()
        raise JobYielded("source_maintenance")

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, stale_yield_handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    running = asyncio.create_task(queue._run_job(job_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    paused_payload = {"checkpoint_version": 11}
    async with SessionLocal() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.PAUSED, payload=paused_payload)
        )
        await session.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.PAUSING)
        )
        await session.commit()

    yield_now.set()
    await asyncio.wait_for(running, timeout=1)

    async with SessionLocal() as session:
        paused_job = await session.get(Job, job_id)
        paused_document = await session.get(Document, document_id)
        assert paused_job.status == JobStatus.PAUSED
        assert paused_job.payload == paused_payload
        assert paused_document.status == DocumentStatus.PAUSED


@pytest.mark.asyncio
async def test_stale_pause_signal_cannot_pause_a_new_worker_claim(monkeypatch):
    from sqlalchemy import update

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    old_entered = asyncio.Event()
    pause_old = asyncio.Event()
    new_entered = asyncio.Event()
    finish_new = asyncio.Event()
    handler_calls = 0

    async def handler(*_args, **_kwargs):
        nonlocal handler_calls
        handler_calls += 1
        if handler_calls == 1:
            old_entered.set()
            await pause_old.wait()
            raise JobPaused
        new_entered.set()
        await finish_new.wait()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=2)
    old_worker = asyncio.create_task(queue._run_job(job_id))
    await asyncio.wait_for(old_entered.wait(), timeout=1)

    async with SessionLocal() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.QUEUED,
                started_at=None,
                payload={"resume_requested": True},
            )
        )
        await session.commit()

    new_worker = asyncio.create_task(queue._run_job(job_id))
    await asyncio.wait_for(new_entered.wait(), timeout=1)
    async with SessionLocal() as session:
        newly_claimed = await session.get(Job, job_id)
        new_claim_started_at = newly_claimed.started_at
        assert newly_claimed.status == JobStatus.RUNNING
        assert new_claim_started_at is not None

    pause_old.set()
    await asyncio.wait_for(old_worker, timeout=1)
    async with SessionLocal() as session:
        still_claimed = await session.get(Job, job_id)
        assert still_claimed.status == JobStatus.RUNNING
        assert still_claimed.started_at == new_claim_started_at

    finish_new.set()
    await asyncio.wait_for(new_worker, timeout=1)
    async with SessionLocal() as session:
        completed = await session.get(Job, job_id)
        assert completed.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_durable_enqueue_keeps_retrying_until_dispatch_succeeds(monkeypatch):
    from sag_api.jobs.inproc import InProcessAsyncQueue

    queue = InProcessAsyncQueue(None, engine_manager=None, concurrency=0)
    attempts = 0
    delivered = asyncio.Event()

    async def flaky_enqueue(job_id):
        nonlocal attempts
        assert job_id == "durable-job"
        attempts += 1
        if attempts < 3:
            raise RuntimeError("queue temporarily unavailable")
        delivered.set()

    monkeypatch.setattr(queue, "enqueue", flaky_enqueue)
    monkeypatch.setattr("sag_api.jobs.inproc._RETRY_ENQUEUE_RETRY_SECONDS", 0.0)

    await queue.enqueue_durably("durable-job")
    await asyncio.wait_for(delivered.wait(), timeout=1)

    assert attempts == 3
    await queue.stop()


@pytest.mark.asyncio
async def test_delete_retry_keeps_document_deleting():
    document = SimpleNamespace(status=DocumentStatus.DELETING, error=None)

    class FakeSession:
        async def get(self, _model, document_id):
            assert document_id == "document-1"
            return document

    await _mark_document_waiting_retry(
        FakeSession(),
        SimpleNamespace(document_id="document-1", type=JobType.DELETE_DOCUMENT),
    )

    assert document.status == DocumentStatus.DELETING


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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["permanent", "attempts_exhausted"])
async def test_delete_job_stops_retrying_and_remains_hidden_on_terminal_failure(
    monkeypatch,
    failure_kind,
):
    from sag_api.core.config import settings
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS
    from sag_api.services.document_service import list_documents

    await init_db()
    initial_attempts = (
        settings.job_max_attempts - 1
        if failure_kind == "attempts_exhausted"
        else 0
    )
    message = (
        "cleanup temporarily unavailable"
        if failure_kind == "attempts_exhausted"
        else "invalid cleanup target"
    )
    async with SessionLocal() as session:
        source = Source(
            name=f"terminal-delete-{failure_kind}",
            sag_source_config_id=f"terminal-delete-{failure_kind}-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="hidden-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/hidden-delete.md",
            status=DocumentStatus.DELETING,
            sag_source_id="derived-hidden-delete",
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            attempts=initial_attempts,
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id

    async def fail_cleanup(*_args, **_kwargs):
        if failure_kind == "attempts_exhausted":
            raise ServiceUnavailableError(message)
        raise ValueError(message)

    monkeypatch.setitem(TASK_HANDLERS, JobType.DELETE_DOCUMENT, fail_cleanup)
    scheduled: list[str] = []
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    monkeypatch.setattr(
        queue,
        "_schedule_retry",
        lambda queued_job_id, _delay: scheduled.append(queued_job_id),
    )

    try:
        await queue._run_job(job_id)

        async with SessionLocal() as session:
            failed_job = await session.get(Job, job_id)
            hidden_document = await session.get(Document, document_id)
            visible_documents = await list_documents(session, source_id)
            assert failed_job.status == JobStatus.FAILED
            assert failed_job.attempts == initial_attempts + 1
            assert failed_job.error == message
            assert hidden_document.status == DocumentStatus.DELETE_FAILED
            assert hidden_document.error == message
            assert document_id not in {
                document.id for document in visible_documents
            }
            assert scheduled == []
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_reprocess_failure_cannot_override_concurrent_delete(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS
    from sag_api.services.document_service import delete_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-wins-reprocess-failure",
            sag_source_config_id=f"delete-wins-reprocess-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            sag_source_id="old-derived-source",
        )
        session.add(document)
        await session.flush()
        reprocess = Job(
            type=JobType.REPROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"derived_source_ids": ["old-derived-source"]},
        )
        session.add(reprocess)
        await session.commit()
        source_id, document_id, reprocess_id = source.id, document.id, reprocess.id

    entered = asyncio.Event()
    fail_now = asyncio.Event()

    async def failing_reprocess(*_args, **_kwargs):
        entered.set()
        await fail_now.wait()
        raise ValueError("invalid reprocess payload")

    monkeypatch.setitem(
        TASK_HANDLERS,
        JobType.REPROCESS_DOCUMENT,
        failing_reprocess,
    )
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    running = asyncio.create_task(queue._run_job(reprocess_id))
    await asyncio.wait_for(entered.wait(), timeout=1)

    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        delete_job = await delete_document(
            session,
            source,
            document_id,
            job_queue=None,
        )
        assert delete_job.status == JobStatus.QUEUED

    fail_now.set()
    await asyncio.wait_for(running, timeout=1)

    async with SessionLocal() as session:
        failed_reprocess = await session.get(Job, reprocess_id)
        deleting = await session.get(Document, document_id)
        assert failed_reprocess.status == JobStatus.FAILED
        assert deleting.status == DocumentStatus.DELETING
        assert deleting.error is None
    await queue.stop()
