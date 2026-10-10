"""Persisted document controls, serial admission and restart side effects."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import delete

from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ConflictError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.services.document_service import pause_document, resume_document
from sag_api.services.source_operation_service import acquire_source_processing_lease
from tests.document_requests import (
    UncommittedDocumentManager,
    controller_processing_identity,
    document_receipt,
    document_request,
    processing_parameters,
)

pytestmark = pytest.mark.usefixtures("controller_identity")


@pytest.fixture(autouse=True)
async def cleanup_control_documents():
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Source).where(Source.name == "manual-control"))
        await session.commit()


async def seed_document(*, status=DocumentStatus.EXTRACTING, job_status=JobStatus.RUNNING, source_id=None):
    async with SessionLocal() as session:
        source = await session.get(Source, source_id) if source_id else None
        if source is None:
            source = Source(name="manual-control", sag_source_config_id=uuid4().hex)
            session.add(source)
            await session.flush()
        document = Document(
            source_id=source.id,
            filename="control.md",
            storage_path="/virtual/control.md",
            status=status,
        )
        session.add(document)
        await session.flush()
        request = document_request(source.sag_source_config_id, source_id=uuid4().hex)
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=job_status,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": request.model_dump(mode="json"),
                "control_revision": 1,
            },
        )
        session.add(job)
        await session.commit()
        return source.id, document.id, job.id, request


@pytest.mark.asyncio
async def test_pause_intent_keeps_running_job_until_engine_drain():
    await init_db()
    source_id, document_id, job_id, request = await seed_document()
    async with SessionLocal() as control:
        source = await control.get(Source, source_id)
        await pause_document(control, source, document_id)
    async with SessionLocal() as reader:
        job = await reader.get(Job, job_id)
        document = await reader.get(Document, document_id)
        assert job.status == JobStatus.RUNNING
        assert job.payload["pause_requested"] is True
        assert job.payload["process_request"] == request.model_dump(mode="json")
        assert document.status == DocumentStatus.PAUSING


@pytest.mark.asyncio
async def test_successful_commit_wins_late_pause_and_publishes_truth():
    await init_db()
    source_id, document_id, job_id, request = await seed_document(job_status=JobStatus.QUEUED)

    class CommittedEngine(UncommittedDocumentManager):
        async def process_document(self, _range_id, request_factory, **kwargs):
            bound = await request_factory(None, *processing_parameters())
            async with SessionLocal() as control:
                source = await control.get(Source, source_id)
                await pause_document(control, source, document_id)
            assert await kwargs["should_pause"]()
            return document_receipt(bound, chunk_count=2, event_count=1)

    queue = InProcessAsyncQueue(SessionLocal, CommittedEngine(), concurrency=2)
    await queue._run_job(job_id)
    async with SessionLocal() as reader:
        job = await reader.get(Job, job_id)
        document = await reader.get(Document, document_id)
        assert document.status == DocumentStatus.READY
        assert document.sag_source_id == request.source.descriptor.source_id
        assert document.published_operation_id == request.context.operation_id
        assert job.status == JobStatus.SUCCEEDED
        assert not job.payload.get("pause_requested")


@pytest.mark.asyncio
async def test_serial_source_admission_does_not_block_another_source():
    await init_db()
    source_id = uuid4().hex
    async with acquire_source_processing_lease(SessionLocal, source_id, "first"):
        async with acquire_source_processing_lease(SessionLocal, uuid4().hex, "independent"):
            pass
        with pytest.raises(ConflictError):
            async with acquire_source_processing_lease(
                SessionLocal,
                source_id,
                "second",
                admission_timeout_seconds=0,
            ):
                pytest.fail("same-source second worker entered while first still owns admission")


@pytest.mark.asyncio
async def test_two_worker_queues_pause_a_run_b_and_queue_resumed_a_without_preemption(document_engine_manager):
    await init_db()
    source_id, document_a, job_a, request_a = await seed_document(job_status=JobStatus.QUEUED)
    _, document_b, job_b, request_b = await seed_document(source_id=source_id, job_status=JobStatus.QUEUED)
    _, document_c, job_c, _ = await seed_document(job_status=JobStatus.QUEUED)
    entered_a, entered_b = asyncio.Event(), asyncio.Event()
    drain_a, finish_b = asyncio.Event(), asyncio.Event()
    runs = []

    class ControlledEngine(UncommittedDocumentManager):
        async def process_document(self, _range_id, request_factory, **kwargs):
            request = await request_factory(None, *processing_parameters())
            runs.append(request.source.descriptor.source_id)
            if (
                request.context.operation_id == request_a.context.operation_id
                and runs.count(request_a.source.descriptor.source_id) == 1
            ):
                entered_a.set()
                await drain_a.wait()
                assert await kwargs["should_pause"]()
                return None
            if request.context.operation_id == request_b.context.operation_id:
                entered_b.set()
                await finish_b.wait()
            return document_receipt(request, chunk_count=1)

    engine = ControlledEngine()
    first = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    second = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    active_a = active_b = None
    try:
        active_a = asyncio.create_task(first._run_job(job_a))
        await asyncio.wait_for(entered_a.wait(), 2)
        await second._run_job(job_b)
        async with SessionLocal() as reader:
            waiting_b = await reader.get(Job, job_b)
            assert waiting_b.status == JobStatus.QUEUED and waiting_b.attempts == 0
            assert waiting_b.error is None
        await second._run_job(job_c)
        async with SessionLocal() as reader:
            assert (await reader.get(Document, document_c)).status == DocumentStatus.READY
        async with SessionLocal() as control:
            await pause_document(control, await control.get(Source, source_id), document_a)
        assert not entered_b.is_set()
        drain_a.set()
        await active_a
        active_b = asyncio.create_task(second._run_job(job_b))
        await asyncio.wait_for(entered_b.wait(), 2)
        async with SessionLocal() as control:
            await resume_document(
                control,
                await control.get(Source, source_id),
                document_a,
                job_queue=first,
                engine_manager=document_engine_manager,
            )
        await first._run_job(job_a)
        assert not active_b.done()
        assert runs.count(request_a.source.descriptor.source_id) == 1
        async with SessionLocal() as reader:
            assert (await reader.get(Job, job_a)).status == JobStatus.QUEUED
        finish_b.set()
        await active_b
        await first._run_job(job_a)
        async with SessionLocal() as reader:
            assert (await reader.get(Document, document_a)).status == DocumentStatus.READY
            assert (await reader.get(Document, document_b)).status == DocumentStatus.READY
            assert (await reader.get(Job, job_a)).attempts == 1
            assert (await reader.get(Job, job_b)).attempts == 1
    finally:
        drain_a.set()
        finish_b.set()
        await asyncio.gather(*(task for task in (active_a, active_b) if task), return_exceptions=True)
        await first.stop()
        await second.stop()


class ReadOnlyRecoveryEngine:
    def __init__(self, result):
        self.result = result
        self.status_reads = []

    @asynccontextmanager
    async def use_concurrently(self, _range_id, _source=None):
        yield self

    async def get_operation_status(self, operation_id):
        self.status_reads.append(operation_id)
        return self.result

    async def process_document(self, *_args, **_kwargs):
        pytest.fail("restart advanced document processing")

    async def document_processing_identity(self, *_args, **_kwargs):
        pytest.fail("a complete receipt checked configuration or missing input bytes")

    async def begin_document_maintenance(self, *_args, **_kwargs):
        pytest.fail("restart opened an automatic deletion window")

    async def delete_data(self, *_args, **_kwargs):
        pytest.fail("restart physically deleted data")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running", "pausing", "paused", "prepared", "deleting", "delete_failed"])
async def test_restart_leaves_every_unfinished_document_operation_for_manual_action(state):
    await init_db()
    deleting = state in {"deleting", "delete_failed"}
    if state in {"queued", "running", "prepared"}:
        document_status = DocumentStatus.PENDING if state == "queued" else DocumentStatus.EXTRACTING
    else:
        document_status = DocumentStatus(state)
    original_status = (
        JobStatus.PAUSED if state == "paused" else JobStatus.QUEUED if state == "queued" else JobStatus.RUNNING
    )
    source_id, document_id, job_id, request = await seed_document(status=document_status, job_status=original_status)
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if deleting:
            job.type = JobType.DELETE_DOCUMENT
            job.payload = {"target_document_id": document_id}
        elif state in {"paused", "pausing"}:
            job.payload = {**job.payload, "pause_requested": True}
        await session.commit()
    result = document_receipt(request).model_copy(
        update={"status": "running", "checkpoint": "extract_prepared" if state == "prepared" else "indexed"}
    )
    engine = ReadOnlyRecoveryEngine(result)
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    try:
        await queue._recover()
        await queue.enqueue(job_id)  # stale dispatch must not bypass the persisted gate
        await queue._run_job(job_id)
        assert queue._queue.empty()
        async with SessionLocal() as reader:
            job = await reader.get(Job, job_id)
            document = await reader.get(Document, document_id)
            assert job.status == JobStatus.PAUSED
            assert job.payload["_scheduler"]["blocked_reason"] == "manual_recovery"
            assert job.attempts == 0
            assert document.status == (document_status if deleting else DocumentStatus.PAUSED)
            if not deleting:
                assert job.payload["process_request"] == request.model_dump(mode="json")
            if state in {"paused", "pausing"}:
                assert job.payload["pause_requested"] is True
        assert engine.status_reads == ([] if deleting else [request.context.operation_id])
        assert not queue.source_maintenance_requested(source_id)
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_restart_reconciles_only_an_already_committed_receipt_once():
    await init_db()
    source_id, document_id, job_id, request = await seed_document()
    engine = ReadOnlyRecoveryEngine(document_receipt(request, chunk_count=2, event_count=1))
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    await queue._recover()
    await queue._recover()
    assert queue._queue.empty()
    async with SessionLocal() as reader:
        document = await reader.get(Document, document_id)
        job = await reader.get(Job, job_id)
        source = await reader.get(Source, source_id)
        assert document.status == DocumentStatus.READY
        assert document.sag_source_id == request.source.descriptor.source_id
        assert job.status == JobStatus.SUCCEEDED
        assert source.chunk_count == 2 and source.event_count == 1
    assert engine.status_reads == [request.context.operation_id]


@pytest.mark.asyncio
async def test_manual_resume_reconciles_a_complete_receipt_before_restart_confirmation():
    """A complete receipt is publishable even when the original bytes are gone."""
    await init_db()
    source_id, document_id, job_id, request = await seed_document(
        status=DocumentStatus.PAUSED,
        job_status=JobStatus.PAUSED,
    )
    engine = ReadOnlyRecoveryEngine(document_receipt(request, chunk_count=2, event_count=1))

    class Queue:
        async def enqueue(self, _job_id):
            pytest.fail("a completed operation was queued again")

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.payload = {**job.payload, "restart_required": "resume_configuration_changed"}
        await session.commit()
        result = await resume_document(
            session,
            await session.get(Source, source_id),
            document_id,
            job_queue=Queue(),
            engine_manager=engine,
        )
        assert result.id == job_id and result.status == JobStatus.SUCCEEDED
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        source = await session.get(Source, source_id)
        assert document.status == DocumentStatus.READY
        assert document.published_operation_id == request.context.operation_id
        assert source.chunk_count == 2 and source.event_count == 1
        assert "restart_confirmation" not in (await session.get(Job, job_id)).payload
    assert engine.status_reads == [request.context.operation_id]


@pytest.mark.asyncio
async def test_worker_reconciles_a_complete_receipt_without_parser_or_model_replay(monkeypatch):
    """A lost completion response only needs the existing business publication transaction."""
    from sag_api.jobs import tasks

    await init_db()
    source_id, document_id, job_id, request = await seed_document()
    engine = ReadOnlyRecoveryEngine(document_receipt(request, chunk_count=3, event_count=2))

    async def prepare(*_args, **_kwargs):
        pytest.fail("a completed operation reparsed missing original bytes")

    monkeypatch.setattr(tasks, "prepare_document", prepare)
    for _ in range(2):
        async with SessionLocal() as session:
            await tasks._process_document_unlocked(
                session, await session.get(Job, job_id), engine_manager=engine, lease_lost=asyncio.Event()
            )
    async with SessionLocal() as session:
        document = await session.get(Document, document_id)
        source = await session.get(Source, source_id)
        assert document.status == DocumentStatus.READY
        assert document.published_operation_id == request.context.operation_id
        assert (await session.get(Job, job_id)).status == JobStatus.SUCCEEDED
        assert source.chunk_count == 3 and source.event_count == 2
    assert engine.status_reads == [request.context.operation_id] * 2


@pytest.mark.asyncio
async def test_unreadable_receipt_cannot_undo_another_documents_reconciliation():
    await init_db()
    _, first_document, first_job, request = await seed_document()
    _, second_document, second_job, _ = await seed_document()
    engine = ReadOnlyRecoveryEngine(document_receipt(request, chunk_count=2))
    queue = InProcessAsyncQueue(SessionLocal, engine)
    await queue._recover()
    async with SessionLocal() as reader:
        assert (await reader.get(Document, first_document)).status == DocumentStatus.READY
        assert (await reader.get(Job, first_job)).status == JobStatus.SUCCEEDED
        assert (await reader.get(Document, second_document)).status == DocumentStatus.PAUSED
        assert (await reader.get(Job, second_job)).payload["_scheduler"]["blocked_reason"] == "manual_recovery"
    assert queue._queue.empty()
