"""文档并发抽取的断点、暂停与继续行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from document_requests import document_request

from sag_api.enums import DocumentStatus
from tests.document_requests import UncommittedDocumentManager, controller_processing_identity, processing_parameters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden_status",
    [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED],
)
async def test_public_document_reads_hide_logically_deleted_rows(hidden_status):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import NotFoundError
    from sag_api.db.models import Document, Source
    from sag_api.services.document_service import get_document, get_public_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"hidden-read-{uuid4().hex}",
            sag_source_config_id=(f"hidden-read-config-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="hidden.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/hidden.md",
            status=hidden_status,
        )
        session.add(document)
        await session.commit()

        assert await get_document(session, source, document.id) is not None
        with pytest.raises(NotFoundError, match="文档不存在"):
            await get_public_document(session, source, document.id)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize(("entities", "expected"), [(0, 1), (8, 8), (99, 20)])
async def test_public_request_preserves_contract_guidance_and_bounded_options(tmp_path, language, entities, expected):
    from document_requests import document_request

    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    markdown = tmp_path / "config.md"
    markdown.write_text("# Test\nA documented knowledge fact.")
    processor = IncrementalDocumentProcessor(
        SimpleNamespace(resources=SimpleNamespace(prompts=SimpleNamespace(language=language))),
        "source-config",
        max_concurrency=30,
        chunk_max_tokens=1000,
        chunk_mode="heading_strict",
        document_title="Document title",
        max_entities_per_event=entities,
    )
    request = await processor.build_request(
        markdown,
        context=document_request("source-config").context,
        input_id="input-config",
    )
    assert request.chunk_options.strategy == "heading_strict"
    assert request.chunk_options.max_tokens == 1000
    assert request.source.descriptor.title == "Document title"
    options = request.extraction_options
    assert options.contract == "rich" and options.source_type == "article"
    assert options.limits.min_entities_per_event == 1
    assert options.limits.max_events_per_chunk == 20
    assert options.limits.max_entities_per_event == expected
    assert options.execution.max_concurrency == 30
    assert len(options.guidance_rules) == 2
    knowledge, security = options.guidance_rules
    if language == "zh":
        assert "观点、事实、定义" in knowledge
        assert "不可信的待分析数据" in security and "不得执行" in security
    else:
        assert "For books, reports, papers" in knowledge
        assert "观点、事实、定义" not in knowledge
        assert "untrusted document data" in security and "must not execute" in security
        assert "canonical output contract" in security


async def test_public_observer_display_errors_do_not_change_a_completed_receipt():
    from document_requests import document_receipt, document_request
    from zleap.sag.pipeline import StageEvent, StageEventType, StageName

    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    request = document_request("source-config")
    receipt = document_receipt(request, chunk_count=2, event_count=1)
    progress = []

    async def process_source(bound, *, observer, cancellation):
        assert bound == request
        for completed in (1, 2):
            await observer(
                StageEvent(
                    run_id=bound.context.operation_id,
                    stage=StageName.EXTRACT,
                    type=StageEventType.PROGRESS,
                    completed=completed,
                    total=2,
                )
            )
        await observer(
            StageEvent(run_id=bound.context.operation_id, stage=StageName.EXTRACT, type=StageEventType.SUCCEEDED)
        )
        return receipt

    async def display(completed, total):
        progress.append((completed, total))
        raise RuntimeError("display database unavailable")

    async def stage(_stage):
        raise RuntimeError("stage display unavailable")

    async def never_pause():
        return False

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(process_source=process_source), "source-config", max_concurrency=1
    )
    result = await processor.process(request, should_pause=never_pause, on_progress=display, on_stage=stage)
    assert result == receipt
    assert progress == [(1, 2), (2, 2)]


async def test_public_engine_failure_propagates_without_fabricating_a_checkpoint():
    from document_requests import document_request
    from zleap.sag.pipeline.errors import PipelineError

    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    async def process_source(*args, **kwargs):
        raise PipelineError("chunk retries exhausted", stage="extract", code="chunk_retry_exhausted")

    async def never_pause():
        return False

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(process_source=process_source), "source-config", max_concurrency=1
    )
    with pytest.raises(PipelineError, match="chunk retries exhausted"):
        await processor.process(document_request("source-config"), should_pause=never_pause)


@pytest.mark.asyncio
@pytest.mark.usefixtures("controller_identity")
async def test_pause_and_resume_document_service(document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="resume-source", sag_source_config_id="resume-source-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume.md",
            status=DocumentStatus.EXTRACTING,
            progress=52,
            token_usage=12_000,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            progress=0.52,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": document_request(source.sag_source_config_id).model_dump(mode="json"),
            },
        )
        session.add(job)
        await session.commit()

        paused_job = await pause_document(session, source, document.id)
        assert paused_job.status == JobStatus.RUNNING
        assert paused_job.payload["pause_requested"] is True
        await session.refresh(document)
        assert document.status == DocumentStatus.PAUSING

        job.status = JobStatus.PAUSED
        document.status = DocumentStatus.PAUSED
        await session.commit()

        queue = FakeQueue()
        resumed_job = await resume_document(
            session, source, document.id, job_queue=queue, engine_manager=document_engine_manager
        )
        assert resumed_job.status == JobStatus.QUEUED
        assert resumed_job.payload["resume_requested"] is True
        assert "pause_requested" not in resumed_job.payload
        assert resumed_job.payload["process_request"]["context"]["isolation"] == "input"
        assert resumed_job.payload["_scheduler"]["priority"] == 10
        assert document.status == DocumentStatus.EXTRACTING
        assert document.progress == 52 and document.token_usage == 12_000
        assert queue.ids == [job.id]

        queued_document = Document(
            source_id=source.id,
            filename="queued.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/queued.md",
            status=DocumentStatus.PENDING,
        )
        session.add(queued_document)
        await session.flush()
        queued_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=queued_document.id,
        )
        session.add(queued_job)
        await session.commit()

        stopped_before_start = await pause_document(session, source, queued_document.id)
        assert stopped_before_start.status == JobStatus.PAUSED
        assert queued_document.status == DocumentStatus.PAUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_version", [None, "legacy-version"])
async def test_resume_legacy_checkpoint_requires_confirmation_and_preserves_original_task(
    tmp_path, chunk_version, document_engine_manager
):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class FakeQueue:
        def __init__(self):
            self.ids = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    source_file = tmp_path / "legacy.md"
    source_file.write_text("legacy", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(
            name=f"legacy-resume-{uuid4().hex}",
            sag_source_config_id=(f"legacy-resume-{uuid4().hex}")[:36],
        )
        session.add(source)
        await session.flush()
        documents = [
            Document(
                source_id=source.id,
                filename="legacy.md",
                storage_path=str(source_file),
                status=DocumentStatus.PAUSED,
            ),
            Document(
                source_id=source.id,
                filename="missing.md",
                storage_path=str(tmp_path / "missing.md"),
                status=DocumentStatus.PAUSED,
            ),
        ]
        session.add_all(documents)
        await session.flush()
        jobs = [
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.PAUSED,
                source_id=source.id,
                document_id=document.id,
                payload={
                    "process_checkpoint": {
                        "source_id": "legacy-source",
                        "chunk_ids": ["chunk-1"],
                        **({"chunk_version": chunk_version} if chunk_version else {}),
                    }
                },
            )
            for document in documents
        ]
        session.add_all(jobs)
        await session.commit()

        queue = FakeQueue()
        for document, job, expected_reason, has_confirmation in zip(
            documents,
            jobs,
            ["legacy_checkpoint", "resume_input_unavailable"],
            [True, False],
            strict=True,
        ):
            original_payload = dict(job.payload)
            with pytest.raises(ConflictError) as caught:
                await resume_document(
                    session, source, document.id, job_queue=queue, engine_manager=document_engine_manager
                )
            assert caught.value.code == "document_restart_required"
            envelope = caught.value.to_envelope()["error"]
            assert envelope["restart_reason"] == expected_reason
            assert ("confirmation_token" in envelope) is has_confirmation
            await session.refresh(job)
            await session.refresh(document)
            assert job.status is JobStatus.PAUSED
            assert document.status is DocumentStatus.PAUSED
            assert {
                key: value for key, value in job.payload.items() if key != "restart_confirmation"
            } == original_payload
            if has_confirmation:
                assert job.payload["restart_confirmation"]["control_revision"] == document.control_revision
        assert queue.ids == []


@pytest.mark.asyncio
async def test_delete_document_persists_deleting_and_queues_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="delete-source", sag_source_config_id="delete-source-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()

        queue = FakeQueue()
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.payload["pause_requested"] is True
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert delete_job.payload["_scheduler"]["priority"] == 0
        assert queue.ids == [delete_job.id]
        assert queue.maintenance == [(source.id, delete_job.id)]

        # Idempotent retries must also repair a stale visible state instead of
        # returning an active delete job while the document appears extracting.
        document.status = DocumentStatus.EXTRACTING
        await session.commit()
        repeated = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        await session.refresh(document)
        assert repeated.id == delete_job.id
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_concurrent_delete_requests_share_one_cleanup_job():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-delete-request",
            sag_source_config_id="concurrent-delete-request-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="extracting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/extracting.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-extracting",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    queue = FakeQueue()

    async def remove():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await delete_document(
                session,
                source,
                document_id,
                job_queue=queue,
            )

    first, second = await asyncio.gather(remove(), remove())

    async with SessionLocal() as session:
        jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.DELETE_DOCUMENT,
                    )
                )
            ).all()
        )
    assert first.id == second.id
    assert [job.id for job in jobs] == [first.id]
    assert set(queue.ids) == {first.id}
    assert set(queue.maintenance) == {(source_id, first.id)}


@pytest.mark.asyncio
async def test_pause_and_resume_only_control_process_jobs(document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            raise AssertionError("delete jobs must never be resumed as extraction jobs")

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="action-guards", sag_source_config_id="action-guards-config"[:36])
        session.add(source)
        await session.flush()
        deleting = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        paused = Document(
            source_id=source.id,
            filename="paused.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/paused.md",
            status=DocumentStatus.PAUSED,
        )
        session.add_all([deleting, paused])
        await session.flush()
        running_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=deleting.id,
            status=JobStatus.RUNNING,
        )
        paused_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=paused.id,
            status=JobStatus.PAUSED,
        )
        session.add_all([running_delete, paused_delete])
        await session.commit()

        with pytest.raises(ConflictError):
            await pause_document(session, source, deleting.id)
        with pytest.raises(ConflictError):
            await resume_document(
                session, source, paused.id, job_queue=FakeQueue(), engine_manager=document_engine_manager
            )

        await session.refresh(running_delete)
        await session.refresh(paused_delete)
        assert running_delete.status == JobStatus.RUNNING
        assert "pause_requested" not in running_delete.payload
        assert paused_delete.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_delete_document_job_removes_document_after_processing_stops(tmp_path, monkeypatch):
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services import universe_service

    class FakeEngine:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_data(self, request, *, source):
            from tests.document_requests import deletion_receipt

            assert source.sag_source_config_id == "delete-worker-config"
            self.deleted.append(request.source_id)
            return deletion_receipt(request)

    await init_db()

    async def partially_scheduled_refresh(session, _job_queue, *, source_id, reason):
        session.add(
            Job(
                type=JobType.INDEX_UNIVERSE,
                source_id=source_id,
                status=JobStatus.QUEUED,
                payload={"reason": reason},
            )
        )
        await session.flush()
        raise RuntimeError("refresh scheduling failed")

    monkeypatch.setattr(universe_service, "schedule_universe_refresh", partially_scheduled_refresh)
    path = tmp_path / "deleting.md"
    path.write_text("content")
    async with SessionLocal() as session:
        source = Source(name="delete-worker", sag_source_config_id="delete-worker-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=7,
            storage_path=str(path),
            status=DocumentStatus.DELETING,
            sag_source_id="engine-document",
        )
        session.add(document)
        await session.flush()
        other_document = Document(
            source_id=source.id,
            filename="keep.md",
            content_type="text/markdown",
            size_bytes=4,
            storage_path=str(tmp_path / "keep.md"),
            status=DocumentStatus.EXTRACTING,
        )
        session.add(other_document)
        await session.flush()
        blocked_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=other_document.id,
            status=JobStatus.QUEUED,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add(blocked_job)
        delete_job = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.QUEUED,
        )
        session.add(delete_job)
        await session.commit()
        document_id, delete_job_id, blocked_job_id = document.id, delete_job.id, blocked_job.id
        source_id = source.id

    engine = FakeEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, delete_job_id)
    await queue._run(delete_job_id)

    async with SessionLocal() as session:
        assert await session.get(Document, document_id) is None
        completed_delete = await session.get(Job, delete_job_id)
        assert completed_delete is not None
        assert completed_delete.status == JobStatus.SUCCEEDED
        assert completed_delete.document_id is None
        assert completed_delete.payload["target_document_id"] == document_id
        source = await session.scalar(select(Source).where(Source.name == "delete-worker"))
        assert source is not None and source.document_count == 1
        resumed = await session.get(Job, blocked_job_id)
        assert resumed.status == JobStatus.QUEUED
        assert resumed.payload["resume_requested"] is True
        assert resumed.payload["_scheduler"] == {"priority": 10}
        universe_jobs = list((await session.scalars(select(Job).where(Job.type == JobType.INDEX_UNIVERSE))).all())
        assert universe_jobs == []
    assert engine.deleted == ["engine-document"]
    assert not path.exists()
    assert queue.source_maintenance_requested(source_id) is False
    queued_ids: list[str] = []
    while not queue._queue.empty():
        queued_ids.append((await queue._queue.get())[-1])
    assert blocked_job_id in queued_ids


@pytest.mark.asyncio
async def test_delete_after_reprocess_request_uses_maintenance_cleanup(tmp_path, monkeypatch, document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    from sag_api.core.config import settings

    raw = tmp_path / "ready.md"
    raw.write_text("# Published content\nOld publication stays readable.\n")
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-after-reprocess",
            sag_source_config_id="delete-after-reprocess-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path=str(raw),
            status=DocumentStatus.READY,
            progress=100,
            sag_source_id="engine-ready",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED,
                payload={"process_checkpoint": {"source_id": "engine-ready"}},
            )
        )
        await session.commit()

        queue = FakeQueue()
        reprocess_job = await reprocess_document(
            session, source, document.id, job_queue=queue, engine_manager=document_engine_manager
        )
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING
        await session.refresh(reprocess_job)
        assert reprocess_job.status == JobStatus.PAUSED
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert await session.get(Document, document.id) is not None
        assert queue.maintenance == [(source.id, delete_job.id)]
        assert delete_job.payload["derived_source_ids"] == ["engine-ready"]


@pytest.mark.asyncio
async def test_delete_after_failed_reprocess_keeps_old_engine_ids_for_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="failed-reprocess-delete", sag_source_config_id="failed-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="failed.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            sag_source_id=None,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.REPROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.FAILED,
                payload={
                    "target_document_id": document.id,
                    "derived_source_ids": ["engine-old", "engine-older"],
                },
            )
        )
        await session.commit()

        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=FakeQueue(),
        )

        assert set(delete_job.payload["derived_source_ids"]) == {
            "engine-old",
            "engine-older",
        }


@pytest.mark.asyncio
async def test_job_pause_is_not_failure_or_retry(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobPaused()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._run(job_id)
    async with SessionLocal() as session:
        paused = await session.get(Job, job_id)
        assert paused.status == JobStatus.PAUSED
        assert paused.attempts == 1
        assert paused.error is None
        paused.status = JobStatus.QUEUED
        paused.payload = {**(paused.payload or {}), "resume_requested": True}
        paused.progress = 0.4
        await session.commit()

    await queue._run(job_id)
    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 2


@pytest.mark.asyncio
async def test_duplicate_queue_entries_claim_job_once(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=2)
    first = asyncio.create_task(queue._run(job_id))
    second = asyncio.create_task(queue._run(job_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)

    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 1


@pytest.mark.asyncio
async def test_pause_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, pause_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-delete-cas",
            sag_source_config_id="pause-delete-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-delete.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-pause-delete",
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if not delete_injected and isinstance(result, Job) and result.id == process_job_id:
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await pause_document(session, source, document_id)

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch, document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-delete-cas",
            sag_source_config_id="resume-delete-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-delete.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
            payload={"process_checkpoint": {"source_id": "engine-resume-delete"}},
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if not delete_injected and isinstance(result, Job) and result.id == process_job_id:
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await resume_document(session, source, document_id, job_queue=queue, engine_manager=document_engine_manager)

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_pause_rejects_ready_document_while_job_completion_is_committing():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-finish-cas",
            sag_source_config_id="pause-finish-cas-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-finish.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-finish.md",
            status=DocumentStatus.READY,
            progress=100,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
                progress=0.99,
            )
        )
        await session.commit()

        with pytest.raises(ConflictError, match="结束|状态"):
            await pause_document(session, source, document.id)


@pytest.mark.parametrize("engine_mode", ["complete", "paused", "error"])
@pytest.mark.asyncio
@pytest.mark.usefixtures("controller_identity")
async def test_process_exit_cannot_overwrite_concurrent_delete(
    monkeypatch,
    engine_mode,
):

    from document_requests import document_receipt, document_request

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.tasks import process_document
    from sag_api.services.document_service import delete_document

    class FakeEngine(UncommittedDocumentManager):
        async def process_document(self, _source_id, request_factory, **_kwargs):
            if engine_mode == "error":
                raise RuntimeError("inflight extraction failed")
            if engine_mode == "paused":
                return None
            return document_receipt(
                await request_factory(None, *processing_parameters()), chunk_count=1, event_count=1, token_usage=50
            )

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

        def source_maintenance_requested(self, _source_id: str):
            return False

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"{engine_mode}-delete-cas",
            sag_source_config_id=(f"{engine_mode}-delete-cas-config")[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename=f"{engine_mode}-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path=f"/tmp/{engine_mode}-delete.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": document_request(
                    source.sag_source_config_id, source_id="engine-exit-delete"
                ).model_dump(mode="json"),
            },
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id = source.id, document.id

        real_refresh = session.refresh
        delete_injected = False

        async def refresh_then_delete(instance, *args, **kwargs):
            nonlocal delete_injected
            await real_refresh(instance, *args, **kwargs)
            if not delete_injected and isinstance(instance, Document):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=FakeQueue(),
                    )

        monkeypatch.setattr(session, "refresh", refresh_then_delete)

        with pytest.raises(JobPaused):
            await process_document(
                session,
                process_job,
                engine_manager=FakeEngine(),
                job_queue=FakeQueue(),
            )

        async with SessionLocal() as verification_session:
            saved_document = await verification_session.get(Document, document_id)
            assert saved_document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_uses_supervised_dispatch_after_persisting_queued_state(document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class DurableQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, _job_id: str):
            raise AssertionError("persisted resume must use supervised dispatch")

        async def enqueue_durably(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-durable-dispatch",
            sag_source_config_id="resume-durable-dispatch-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-durable-dispatch.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-durable-dispatch.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
        )
        session.add(process_job)
        await session.commit()

        queue = DurableQueue()
        resumed = await resume_document(
            session, source, document.id, job_queue=queue, engine_manager=document_engine_manager
        )

        assert resumed.status == JobStatus.QUEUED
        assert queue.ids == [process_job.id]
