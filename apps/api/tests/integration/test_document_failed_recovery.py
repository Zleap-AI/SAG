"""Manual recovery reuses durable work after the automatic retry budget is spent."""

from copy import deepcopy
from uuid import uuid4

import pytest

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.inproc import InProcessAsyncQueue
from sag_api.sag import EngineManager
from sag_api.services.document_service import reprocess_document
from tests.document_requests import (
    UncommittedDocumentManager,
    controller_processing_identity,
    document_receipt,
    document_request,
)
from tests.test_document_durable_processing import admitted_document_task as admitted_document_task


async def test_manual_recovery_after_retry_exhaustion_reuses_successful_chunks(
    admitted_document_task, monkeypatch,
):
    session, old_manager, stock, models, source, document, job, raw = admitted_document_task
    source_id, document_id, job_id, range_id = source.id, document.id, job.id, source.sag_source_config_id
    await old_manager.aclose_all()
    raw.write_text("\n\n".join(
        f"# Section {index}\nAcme Corporation completed migration milestone {index}. "
        + f"Milestone {index} includes validation and a separately recorded operational result. " * 20
        for index in range(6)
    ), encoding="utf-8")
    manager = EngineManager(stock.model_copy(deep=True, update={"document_extract_concurrency": 1}))
    original_process = manager.process_document
    inject_outage = True

    async def process_with_outage_receipt(range_id, request_factory, **kwargs):
        cancel = False
        on_progress, should_pause = kwargs["on_progress"], kwargs["should_pause"]

        async def checkpoint_saved(done, total):
            nonlocal cancel
            await on_progress(done, total)
            if inject_outage and done >= 1:
                cancel = True

        async def stop_after_checkpoint():
            return cancel or await should_pause()

        result = await original_process(range_id, request_factory, **{
            **kwargs, "on_progress": checkpoint_saved, "should_pause": stop_after_checkpoint,
        })
        if inject_outage:
            assert result.failure_code == "cancelled"
            assert result.extraction_progress["succeeded_chunk_ids"]
            # Fault injection at the public manager receipt only: the installed
            # SDK, HTTP models, SQLite metadata and LanceDB checkpoints are real.
            return result.model_copy(update={
                "failure_code": "dependency_unavailable",
                "failure_message": "Injected temporary outage after a durable chunk checkpoint",
                "retryable": True,
            })
        return result

    async def no_projection_refresh(*args, **kwargs):
        return []

    monkeypatch.setattr(manager, "process_document", process_with_outage_receipt)
    monkeypatch.setattr("sag_api.services.universe_service.schedule_universe_refresh", no_projection_refresh)
    # Exercise the final ordinary business attempt without spending the real
    # backoff intervals on preceding failures. Do not alter the retry policy.
    job.status = JobStatus.QUEUED
    job.attempts = settings.job_max_attempts - 1
    await session.commit()
    await session.rollback()
    queue = InProcessAsyncQueue(SessionLocal, manager, concurrency=1)
    try:
        await queue._run_job(job_id)
        async with SessionLocal() as reader:
            failed_job = await reader.get(Job, job_id)
            failed_document = await reader.get(Document, document_id)
            assert failed_job.status is JobStatus.FAILED
            assert failed_job.attempts == settings.job_max_attempts
            assert failed_document.status is DocumentStatus.FAILED and not failed_document.has_publication
            request = deepcopy(failed_job.payload["process_request"])
            revision = failed_document.control_revision
        operation_id = request["context"]["operation_id"]
        input_id = request["source"]["descriptor"]["source_id"]
        async with manager.use(range_id) as engine:
            partial = await engine.get_operation_status(operation_id)
        succeeded_chunks = set(partial.extraction_progress["succeeded_chunk_ids"])
        total_chunks = partial.extraction_progress["chunk_total"]
        assert 0 < len(succeeded_chunks) < total_chunks
        saved_signatures = list(models.calls)
        assert len(saved_signatures) == len(succeeded_chunks)

        inject_outage = False
        async with SessionLocal() as control:
            recovered = await reprocess_document(
                control, await control.get(Source, source_id), document_id,
                job_queue=queue, engine_manager=manager,
            )
            assert recovered.id == job_id, "a retryable FAILED task must reuse its original durable operation"
            assert recovered.payload["process_request"] == request
            assert recovered.payload["control_revision"] == revision
            assert recovered.attempts < settings.job_max_attempts
        await queue._run_job(job_id)

        async with SessionLocal() as reader:
            finished_job = await reader.get(Job, job_id)
            published_document = await reader.get(Document, document_id)
            assert finished_job.status is JobStatus.SUCCEEDED
            assert finished_job.payload["process_request"] == request
            assert published_document.status is DocumentStatus.READY and published_document.has_publication
            assert published_document.control_revision == revision
            assert published_document.published_operation_id == operation_id
            assert published_document.sag_source_id == input_id
        async with manager.use(range_id) as engine:
            completed = await engine.get_operation_status(operation_id)
        assert completed.status == "succeeded" and completed.checkpoint == "ready"
        assert succeeded_chunks <= set(completed.extraction_progress["succeeded_chunk_ids"])
        assert len(models.calls) - len(saved_signatures) == total_chunks - len(succeeded_chunks)
        assert all(models.calls.count(signature) == 1 for signature in saved_signatures)
    finally:
        models.release.set()
        await queue.stop()
        await manager.aclose_all()


@pytest.fixture
async def postgres_business_sessions():
    """Each case owns one disposable PostgreSQL business database, never the global DB."""
    import os

    port = os.environ.get("SAG_DOCUMENT_TEST_PG_PORT")
    if not port:
        pytest.skip("disposable PostgreSQL endpoint not explicitly configured")
    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from sag_api.db.base import Base

    database = f"document_recovery_{uuid4().hex}"
    admin = await asyncpg.connect(
        host="127.0.0.1", port=int(port), user="sag_isolation",
        password="local-test-only", database="sag_isolation", timeout=10,
    )
    engine = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        created = True
        engine = create_async_engine(
            f"postgresql+asyncpg://sag_isolation:local-test-only@127.0.0.1:{port}/{database}",
            poolclass=NullPool,
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if engine is not None:
                await engine.dispose()
            if created:
                await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.mark.parametrize("recovery", ["parser_retry", "committed_receipt", "changed_input"])
async def test_failed_recovery_controls_work_with_postgresql_business_storage(
    postgres_business_sessions, tmp_path, monkeypatch, recovery,
):
    """Exercise payload compare-and-swap against PostgreSQL's actual JSON column."""
    from sag_api.core.errors import DocumentRestartRequiredError

    class RecordingQueue:
        def __init__(self):
            self.ids = []

        async def enqueue(self, job_id):
            self.ids.append(job_id)

    raw = tmp_path / "report.md"
    raw.write_text("# Report\nAn original input with a saved parser task.\n", encoding="utf-8")
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    queue = RecordingQueue()
    async with postgres_business_sessions() as session:
        source = Source(name="postgres-document-recovery", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename=raw.name, storage_path=str(raw),
            status=DocumentStatus.FAILED, error="temporary parser outage",
        )
        session.add(document)
        await session.flush()
        parser = {"provider": "mineru", "status": "running", "task_id": "existing-parser-task"}
        job = Job(
            type=JobType.PROCESS_DOCUMENT, status=JobStatus.FAILED,
            source_id=source.id, document_id=document.id, attempts=settings.job_max_attempts,
            payload={
                "control_revision": document.control_revision,
                "failure_retryable": True,
                "processing_identity": controller_processing_identity(raw),
                "document_parser": parser,
            },
        )
        session.add(job)
        await session.commit()
        original_job_id, revision = job.id, document.control_revision
        manager = UncommittedDocumentManager()
        if recovery == "committed_receipt":
            request = document_request(source.sag_source_config_id, source_id="committed-input")
            job.payload = {**job.payload, "process_request": request.model_dump(mode="json")}
            await session.commit()
            raw.unlink()

            class CommittedOperation(UncommittedDocumentManager):
                async def get_operation_status(self, operation_id):
                    assert operation_id == request.context.operation_id
                    return document_receipt(request, chunk_count=2, event_count=1)

                async def document_processing_identity(self, *_args, **_kwargs):
                    pytest.fail("publication reconciliation must not re-read input or configuration")

            manager = CommittedOperation()
        elif recovery == "changed_input":
            raw.write_text("# Replacement\nThe user replaced the input bytes.\n", encoding="utf-8")
            with pytest.raises(DocumentRestartRequiredError) as caught:
                await reprocess_document(
                    session, source, document.id, job_queue=queue, engine_manager=manager,
                )
            assert caught.value.reason == "resume_input_changed" and caught.value.confirmation_token
            assert job.status is JobStatus.PAUSED and document.status is DocumentStatus.PAUSED
            assert job.payload["document_parser"] == parser and document.control_revision == revision
            assert queue.ids == []
            replacement = await reprocess_document(
                session, source, document.id, job_queue=queue, engine_manager=manager,
                confirmation_token=caught.value.confirmation_token,
            )
            assert replacement.id != original_job_id
            assert document.control_revision == revision + 1
            await session.refresh(job)
            assert job.status is JobStatus.FAILED and job.payload["replacement_job_id"] == replacement.id
            assert queue.ids == [replacement.id]
            return

        recovered = await reprocess_document(
            session, source, document.id, job_queue=queue, engine_manager=manager,
        )
        assert recovered.id == original_job_id and document.control_revision == revision
        assert recovered.payload["document_parser"] == parser
        if recovery == "committed_receipt":
            assert recovered.status is JobStatus.SUCCEEDED and document.status is DocumentStatus.READY
            assert document.published_operation_id == request.context.operation_id
            assert document.sag_source_id == request.source.descriptor.source_id
            assert document.chunk_count == 2 and document.event_count == 1
            assert queue.ids == []
        else:
            assert recovered.status is JobStatus.QUEUED and recovered.attempts == 0
            assert document.status is DocumentStatus.PENDING
            assert queue.ids == [original_job_id]
