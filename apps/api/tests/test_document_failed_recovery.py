"""Manual retry preserves a failed document's compatible durable work."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ConflictError, DocumentRestartRequiredError, UpstreamError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.services.document_service import delete_document, reprocess_document, resume_document
from tests.document_requests import (
    UncommittedDocumentManager,
    controller_processing_identity,
    document_receipt,
    document_request,
)


class RecordingQueue:
    def __init__(self):
        self.ids = []

    async def enqueue(self, job_id):
        self.ids.append(job_id)


@pytest.fixture
async def failed_document(tmp_path):
    await init_db()
    raw = tmp_path / "report.md"
    raw.write_text("# Report\nOne saved document input.\n")
    async with SessionLocal() as session:
        source = Source(name="failed-document-recovery", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename=raw.name, storage_path=str(raw),
            size_bytes=raw.stat().st_size, content_type="text/markdown",
            status=DocumentStatus.FAILED, progress=78, token_usage=2300,
            error="parser service unavailable", error_layer="engine", error_stage="parse",
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT, status=JobStatus.FAILED,
            source_id=source.id, document_id=document.id,
            attempts=settings.job_max_attempts, error="parser service unavailable",
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC), progress=0.78,
            payload={
                "failure_retryable": True,
                "control_revision": document.control_revision,
                "processing_identity": controller_processing_identity(raw),
                "input_artifact": {
                    "path": str(raw), "directory": str(tmp_path),
                    "size_bytes": raw.stat().st_size,
                    "sha256": controller_processing_identity(raw)["original_sha256"],
                },
                "document_parser": {
                    "provider": "mineru", "status": "running", "task_id": "existing-parser-task",
                    "batch_id": "existing-parser-batch", "upload_url": "https://parser.example/upload",
                },
                "_scheduler": {"blocked_reason": "manual_recovery"},
            },
        )
        session.add(job)
        await session.commit()
        source_id, document_id, job_id = source.id, document.id, job.id
    yield source_id, document_id, job_id, raw
    async with SessionLocal() as session:
        await session.execute(delete(Source).where(Source.id == source_id))
        await session.commit()


@pytest.mark.asyncio
async def test_reprocess_retryable_parser_failure_reuses_task_and_resets_retry_budget(failed_document):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        original = await session.get(Job, job_id)
        original_payload = dict(original.payload)
        result = await reprocess_document(
            session, await session.get(Source, source_id), document_id,
            job_queue=queue, engine_manager=UncommittedDocumentManager(),
        )
        assert result.id == job_id
        assert result.status == JobStatus.QUEUED and result.attempts == 0
        assert result.started_at is None and result.finished_at is None and result.error is None
        for key in ("document_parser", "input_artifact", "processing_identity", "control_revision"):
            assert result.payload[key] == original_payload[key]
        assert not result.payload.get("failure_retryable")
        assert not result.payload.get("_scheduler", {}).get("blocked_reason")
        document = await session.get(Document, document_id)
        assert document.status == DocumentStatus.PENDING
        assert document.control_revision == 1 and document.progress == 78 and document.token_usage == 2300
        assert document.error is None and document.error_layer is None and document.error_stage is None
        assert queue.ids == [job_id]
        jobs = list(await session.scalars(select(Job).where(Job.document_id == document_id)))
        assert [job.id for job in jobs] == [job_id]


@pytest.mark.asyncio
async def test_repeated_reprocess_of_recovered_failure_returns_the_same_job(failed_document):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        first = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=UncommittedDocumentManager(),
        )
        second = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=UncommittedDocumentManager(),
        )
        assert first.id == second.id == job_id
        assert queue.ids == [job_id]
        assert (await session.get(Document, document_id)).control_revision == 1


@pytest.mark.asyncio
async def test_paused_resume_retains_existing_retry_budget(failed_document):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.PAUSED
        job.payload = {key: value for key, value in job.payload.items() if key != "failure_retryable"}
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.PAUSED
        await session.commit()
        result = await resume_document(
            session, await session.get(Source, source_id), document_id,
            job_queue=queue, engine_manager=UncommittedDocumentManager(),
        )
        assert result.id == job_id and result.status == JobStatus.QUEUED
        assert result.attempts == settings.job_max_attempts
        assert result.payload["document_parser"]["task_id"] == "existing-parser-task"
        assert document.control_revision == 1
        assert queue.ids == [job_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_failure_policy", [False, True])
async def test_retryable_engine_operation_preserves_saved_request_and_checkpoint(failed_document, has_failure_policy):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        request = document_request(source.sag_source_config_id, source_id="retained-document-input")
        job = await session.get(Job, job_id)
        payload = dict(job.payload)
        if not has_failure_policy:
            payload.pop("failure_retryable")
        job.payload = {**payload, "process_request": request.model_dump(mode="json")}
        await session.commit()

        class RetryableOperation(UncommittedDocumentManager):
            async def get_operation_status(self, operation_id):
                assert operation_id == request.context.operation_id
                return document_receipt(request).model_copy(update={
                    "status": "failed", "checkpoint": "indexed", "retryable": True,
                    "failure_code": "dependency_unavailable", "failure_message": "temporary vector outage",
                    "result": None,
                })

        result = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=RetryableOperation(),
        )
        assert result.id == job_id
        assert result.payload["process_request"] == request.model_dump(mode="json")
        assert result.attempts == 0
        document = await session.get(Document, document_id)
        assert document.status == DocumentStatus.EXTRACTING
        assert document.control_revision == 1 and document.progress == 78 and document.token_usage == 2300
        assert queue.ids == [job_id]


@pytest.mark.asyncio
async def test_delete_during_failed_recovery_cannot_revive_the_document(failed_document):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()

    class DeleteDuringIdentityCheck(UncommittedDocumentManager):
        async def document_processing_identity(self, _range_id, path, *, source=None):
            async with SessionLocal() as control:
                await delete_document(control, await control.get(Source, source_id), document_id)
            return controller_processing_identity(path)

    async with SessionLocal() as session:
        with pytest.raises(ConflictError):
            await reprocess_document(
                session, await session.get(Source, source_id), document_id,
                job_queue=queue, engine_manager=DeleteDuringIdentityCheck(),
            )
    async with SessionLocal() as session:
        assert (await session.get(Document, document_id)).status == DocumentStatus.DELETING
        assert (await session.get(Job, job_id)).status != JobStatus.QUEUED
        assert queue.ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["input", "configuration"])
async def test_changed_failed_input_requires_confirmation_without_discarding_parser_work(failed_document, changed):
    source_id, document_id, job_id, raw = failed_document
    queue = RecordingQueue()

    class ChangedConfiguration(UncommittedDocumentManager):
        async def document_processing_identity(self, _range_id, path, *, source=None):
            identity = controller_processing_identity(path)
            if changed == "configuration":
                identity["configuration_digest"] = "c" * 64
            return identity

    if changed == "input":
        raw.write_text("# Replacement\nDifferent input bytes.\n")
    async with SessionLocal() as session:
        original_payload = dict((await session.get(Job, job_id)).payload)
        with pytest.raises(DocumentRestartRequiredError) as error:
            await reprocess_document(
                session, await session.get(Source, source_id), document_id,
                job_queue=queue, engine_manager=ChangedConfiguration(),
            )
        assert error.value.reason == f"resume_{changed}_changed"
        assert error.value.confirmation_token
        await session.refresh(await session.get(Job, job_id))
        job = await session.get(Job, job_id)
        document = await session.get(Document, document_id)
        assert job.status == JobStatus.PAUSED and document.status == DocumentStatus.PAUSED
        assert document.control_revision == 1
        assert job.payload["document_parser"] == original_payload["document_parser"]
        assert job.payload["input_artifact"] == original_payload["input_artifact"]
        assert queue.ids == []
        replacement = await reprocess_document(
            session, await session.get(Source, source_id), document_id,
            job_queue=queue, engine_manager=ChangedConfiguration(),
            confirmation_token=error.value.confirmation_token,
        )
        assert replacement.id != job_id
        assert (await session.get(Document, document_id)).control_revision == 2
        retired = await session.get(Job, job_id, populate_existing=True)
        assert retired.payload["replacement_job_id"] == replacement.id
        assert queue.ids == [replacement.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["permanent", "unknown"])
async def test_unrecoverable_or_unknown_failure_never_silently_restarts(failed_document, policy):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        payload = dict(job.payload)
        if policy == "permanent":
            payload["failure_retryable"] = False
        else:
            payload.pop("failure_retryable")
        job.payload = payload
        await session.commit()
        with pytest.raises(DocumentRestartRequiredError) as error:
            await reprocess_document(
                session, await session.get(Source, source_id), document_id,
                job_queue=queue, engine_manager=UncommittedDocumentManager(),
            )
        assert error.value.confirmation_token
        job = await session.get(Job, job_id)
        document = await session.get(Document, document_id)
        assert job.status == JobStatus.PAUSED and document.status == DocumentStatus.PAUSED
        assert document.control_revision == 1
        assert job.payload["document_parser"]["task_id"] == "existing-parser-task"
        assert queue.ids == []
        assert len(list(await session.scalars(select(Job).where(Job.document_id == document_id)))) == 1


@pytest.mark.asyncio
async def test_permanent_engine_receipt_overrides_retryable_business_failure(failed_document):
    source_id, document_id, job_id, _raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        request = document_request(source.sag_source_config_id)
        job = await session.get(Job, job_id)
        job.payload = {**job.payload, "process_request": request.model_dump(mode="json")}
        await session.commit()

        class FailedOperation(UncommittedDocumentManager):
            async def get_operation_status(self, operation_id):
                assert operation_id == request.context.operation_id
                return document_receipt(request).model_copy(update={
                    "status": "failed", "checkpoint": "indexed", "retryable": False,
                    "failure_code": "invalid_input", "failure_message": "permanent operation failure",
                    "result": None,
                })

        with pytest.raises(DocumentRestartRequiredError) as error:
            await reprocess_document(
                session, source, document_id, job_queue=queue, engine_manager=FailedOperation(),
            )
        assert error.value.confirmation_token
        assert (await session.get(Document, document_id)).control_revision == 1
        assert (await session.get(Job, job_id)).payload["process_request"] == request.model_dump(mode="json")
        assert queue.ids == []


@pytest.mark.asyncio
async def test_failed_response_to_successful_engine_commit_only_reconciles_publication(failed_document):
    source_id, document_id, job_id, raw = failed_document
    queue = RecordingQueue()
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        request = document_request(source.sag_source_config_id)
        job = await session.get(Job, job_id)
        payload = dict(job.payload)
        payload.pop("input_artifact")
        job.payload = {**payload, "process_request": request.model_dump(mode="json")}
        await session.commit()
        raw.unlink()

        class CommittedOperation(UncommittedDocumentManager):
            async def get_operation_status(self, operation_id):
                assert operation_id == request.context.operation_id
                return document_receipt(request, chunk_count=14, event_count=2, token_usage=2400)

            async def document_processing_identity(self, *_args, **_kwargs):
                pytest.fail("a committed result must not require original input or configuration")

        result = await reprocess_document(
            session, source, document_id, job_queue=queue, engine_manager=CommittedOperation(),
        )
        assert result.id == job_id and result.status == JobStatus.SUCCEEDED
        document = await session.get(Document, document_id)
        assert document.status == DocumentStatus.READY
        assert document.published_operation_id == request.context.operation_id
        assert document.chunk_count == 14 and document.event_count == 2
        assert document.control_revision == 1
        assert queue.ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [False, True])
async def test_terminal_worker_failure_persists_manual_recovery_policy(failed_document, monkeypatch, retryable):
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    source_id, document_id, job_id, _raw = failed_document
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.QUEUED
        job.attempts = settings.job_max_attempts - 1
        payload = dict(job.payload)
        payload.pop("failure_retryable")
        payload.pop("_scheduler")
        job.payload = payload
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.EXTRACTING
        await session.commit()

    async def failed_handler(session, job, **_kwargs):
        document = await session.get(Document, job.document_id)
        document.status = DocumentStatus.FAILED
        await session.commit()
        raise UpstreamError("parser temporarily unavailable", retryable=retryable)

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, failed_handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    try:
        await queue._run_job(job_id)
        async with SessionLocal() as session:
            failed = await session.get(Job, job_id)
            assert failed.status == JobStatus.FAILED
            assert failed.payload["failure_retryable"] is retryable
            assert failed.attempts == settings.job_max_attempts
            assert (await session.get(Document, document_id)).status == DocumentStatus.FAILED
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_invalid_recovery_configuration_is_a_domain_error(document_engine_manager, tmp_path):
    from sag_api.core.errors import ValidationError

    source_config_id = uuid4().hex
    await document_engine_manager.provision(source_config_id)
    document_engine_manager._settings.llm_base_url = ""
    raw = tmp_path / "configuration.md"
    raw.write_text("# Document\nA saved input awaiting valid model configuration.\n")
    with pytest.raises(ValidationError) as error:
        await document_engine_manager.document_processing_identity(source_config_id, str(raw))
    assert error.value.layer.value == "engine"
    assert error.value.stage.value == "config"
