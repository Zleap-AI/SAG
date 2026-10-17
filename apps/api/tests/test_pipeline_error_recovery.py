"""Keep engine recovery decisions intact across the API/queue boundary."""

import errno

import httpx
import pytest
from zleap.sag.pipeline.errors import ExtractionError, SourceReadError

from sag_api.core.error_taxonomy import ErrorStage
from sag_api.core.errors import ApiError, ServiceUnavailableError, UpstreamError
from sag_api.jobs.inproc import _is_retryable
from sag_api.sag.errors import map_sag_errors


@pytest.mark.parametrize(
    ("error_type", "code", "stage", "retryable"),
    [
        (SourceReadError, "file_read_failed", "parse", False),
        (ExtractionError, "extraction_rolled_back", "extract", True),
        (ExtractionError, "extraction_rollback_failed", "extract", False),
    ],
)
def test_pipeline_error_keeps_its_recovery_contract(error_type, code, stage, retryable):
    original = error_type("engine failure", code=code, stage=stage, retryable=retryable)
    with pytest.raises(ApiError) as caught:
        with map_sag_errors(stage=ErrorStage.EXTRACT):
            raise original

    translated = caught.value
    assert translated.code == code
    assert translated.stage.value == stage
    assert translated.retryable is retryable
    assert _is_retryable(translated) is retryable
    assert translated.__cause__ is original


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UpstreamError("legacy upstream failure"), True),
        (UpstreamError("unsafe to repeat", retryable=False), False),
        (ServiceUnavailableError("explicitly permanent", retryable=False), False),
        (ApiError("safe to repeat", retryable=True), True),
    ],
)
def test_queue_honors_explicit_retryability_without_changing_legacy_upstream(error, expected):
    assert _is_retryable(error) is expected


@pytest.mark.parametrize("descriptor_errno", [errno.EMFILE, errno.ENFILE])
@pytest.mark.parametrize(
    ("code", "original_retryable", "expected_retryable"),
    [
        ("file_read_failed", False, True),
        ("extraction_rolled_back", True, True),
        ("extraction_rollback_failed", False, False),
    ],
)
def test_descriptor_exhaustion_is_safe_to_report_and_respects_rollback(
    descriptor_errno, code, original_retryable, expected_retryable
):
    cause = OSError(descriptor_errno, "Too many open files", "/private/tenant/secret.md")
    wrapper = RuntimeError("transport failure")
    wrapper.__cause__ = cause
    original = ExtractionError(
        "failed at /private/tenant/secret.md", code=code, stage="extract",
        retryable=original_retryable, cause=wrapper,
    )
    with pytest.raises(ApiError) as caught:
        with map_sag_errors(stage=ErrorStage.EXTRACT):
            raise original

    translated = caught.value
    assert translated.code == code
    assert translated.status_code == 503
    assert translated.retryable is expected_retryable
    assert _is_retryable(translated) is expected_retryable
    assert "/private" not in translated.message
    assert translated.__cause__.__cause__.__cause__ is cause


def test_pipeline_unknown_stage_uses_callers_stage():
    original = ExtractionError("failed", code="custom_engine_failure", stage="future-stage")
    with pytest.raises(ApiError) as caught:
        with map_sag_errors(stage=ErrorStage.EXTRACT):
            raise original
    assert caught.value.stage == ErrorStage.EXTRACT
    assert caught.value.code == "custom_engine_failure"


@pytest.mark.parametrize(("stage", "expected"), [("index", "persist"), ("search", "retrieve")])
def test_pipeline_stage_aliases_match_documented_api_stages(stage, expected):
    with pytest.raises(ApiError) as caught:
        with map_sag_errors():
            raise ExtractionError("failed", stage=stage)
    assert caught.value.stage.value == expected


def test_descriptor_cause_cycles_terminate_and_ignore_non_descriptor_os_errors():
    from sag_api.core.resource_errors import descriptor_exhausted

    first, second = RuntimeError("first"), RuntimeError("second")
    first.__cause__, second.__cause__ = second, first
    assert descriptor_exhausted(first) is False
    assert descriptor_exhausted(OSError(errno.EACCES, "Permission denied")) is False
    with pytest.raises(OSError) as caught:
        with map_sag_errors():
            raise OSError(errno.EACCES, "Permission denied")
    assert caught.value.errno == errno.EACCES


@pytest.mark.parametrize("code", ["index_rollback_failed", "extraction_rollback_failed"])
def test_resource_cause_never_overrides_incomplete_compensation(code):
    cause = OSError(errno.EMFILE, "Too many open files")
    error = ExtractionError("incomplete rollback", code=code, retryable=False, cause=cause)
    with pytest.raises(ApiError) as caught:
        with map_sag_errors():
            raise error
    assert _is_retryable(caught.value) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "retryable", "expected_job_status", "expected_document_status"),
    [
        ("extraction_rolled_back", True, "queued", "pending"),
        ("extraction_rollback_failed", False, "failed", "failed"),
    ],
)
async def test_queue_persists_pipeline_recovery_decision(
    monkeypatch, code, retryable, expected_job_status, expected_document_status
):
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="pipeline-recovery", sag_source_config_id=uuid4().hex)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id, filename="retry.md", storage_path="/tmp/retry.md",
            content_type="text/markdown", size_bytes=16, status=DocumentStatus.EXTRACTING,
            progress=64, token_usage=9000,
        )
        session.add(document)
        await session.flush()
        job = Job(
            source_id=source.id, document_id=document.id, type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED, payload={"checkpoint": {"chunk_ids": ["chunk-1"]}},
        )
        session.add(job)
        await session.commit()
        job_id, document_id = job.id, document.id

    async def fail(session, job, **kwargs):
        document = await session.get(Document, document_id)
        document.status = DocumentStatus.FAILED
        await session.commit()
        with map_sag_errors(stage=ErrorStage.EXTRACT):
            raise ExtractionError("failed", code=code, stage="extract", retryable=retryable)

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, fail)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    scheduled = []
    monkeypatch.setattr(queue, "_schedule_retry", lambda job_id, delay: scheduled.append(job_id))
    await queue._run_job(job_id)

    async with SessionLocal() as session:
        saved_job = await session.get(Job, job_id)
        saved_document = await session.get(Document, document_id)
        assert saved_job.status.value == expected_job_status
        assert saved_document.status.value == expected_document_status
        assert saved_document.progress == 64
        assert saved_document.token_usage == 9000
        assert saved_job.payload == {"checkpoint": {"chunk_ids": ["chunk-1"]}}
    assert scheduled == ([job_id] if retryable else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor_errno", [errno.EMFILE, errno.ENFILE])
async def test_http_descriptor_exhaustion_returns_safe_retryable_response(descriptor_errno):
    from sag_api.main import create_app

    app = create_app()

    @app.get("/descriptor-exhaustion-test")
    async def fail():
        raise OSError(descriptor_errno, "Too many open files", "/private/tenant/secret.md")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/descriptor-exhaustion-test")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "resource_exhausted"
    assert error["retryable"] is True
    assert "/private" not in error["message"]
