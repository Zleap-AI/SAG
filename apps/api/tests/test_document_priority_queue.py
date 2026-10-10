import pytest
from document_requests import DocumentRequestBuilder, document_receipt, document_request

from tests.document_requests import UncommittedDocumentManager, controller_processing_identity, processing_parameters


def _queued_job_id(item) -> str:
    """Priority queue items end with the persisted job id."""
    assert isinstance(item, tuple)
    return item[-1]


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    import asyncio

    async def wait() -> None:
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_delete_job_dequeues_before_an_earlier_normal_job():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        normal = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        delete = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            payload={"_scheduler": {"priority": 0}},
        )
        session.add_all([normal, delete])
        await session.commit()
        normal_id, delete_id = normal.id, delete.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue.enqueue(normal_id)
    await queue.enqueue(delete_id)

    first = await queue._queue.get()
    second = await queue._queue.get()
    assert _queued_job_id(first) == delete_id
    assert _queued_job_id(second) == normal_id


@pytest.mark.asyncio
async def test_enqueue_retries_a_temporary_database_lock(monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.exc import OperationalError

    from sag_api.enums import JobType
    from sag_api.jobs import inproc
    from sag_api.jobs.inproc import InProcessAsyncQueue

    attempts = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, job_id):
            nonlocal attempts
            assert job_id == "locked-job"
            attempts += 1
            if attempts < 3:
                raise OperationalError("SELECT", {}, Exception("database is locked"))
            return SimpleNamespace(type=JobType.SYNC_SOURCE, source_id=None, payload={})

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(inproc.asyncio, "sleep", no_sleep)
    queue = InProcessAsyncQueue(lambda: FakeSession(), engine_manager=None, concurrency=1)
    await queue.enqueue("locked-job")

    assert attempts == 3
    assert _queued_job_id(await queue._queue.get()) == "locked-job"


@pytest.mark.asyncio
async def test_normal_jobs_keep_fifo_order_without_scheduler_metadata():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        first_job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        second_job = Job(type=JobType.SYNC_SOURCE, status=JobStatus.QUEUED)
        session.add_all([first_job, second_job])
        await session.commit()
        first_id, second_id = first_job.id, second_job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue.enqueue(first_id)
    await queue.enqueue(second_id)

    first = await queue._queue.get()
    second = await queue._queue.get()
    assert _queued_job_id(first) == first_id
    assert _queued_job_id(second) == second_id


@pytest.mark.asyncio
async def test_recovery_requires_manual_action_for_document_processing_and_deletion():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        normal = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        delete = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            payload={"_scheduler": {"priority": 0}},
        )
        session.add_all([normal, delete])
        await session.commit()
        normal_id, delete_id = normal.id, delete.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._recover()

    recovered_ids: list[str] = []
    while not queue._queue.empty():
        recovered_ids.append(_queued_job_id(await queue._queue.get()))
    assert recovered_ids == []
    async with SessionLocal() as session:
        assert (await session.get(Job, delete_id)).status == JobStatus.PAUSED
        assert (await session.get(Job, normal_id)).status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_recovery_deduplicates_legacy_active_document_jobs():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="legacy-duplicate-recovery",
            sag_source_config_id="legacy-duplicate-recovery-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="duplicate.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/duplicate.md",
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        jobs = [
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.QUEUED,
                source_id=source.id,
                document_id=document.id,
            )
            for _ in range(3)
        ]
        session.add_all(jobs)
        await session.commit()
        document_id = document.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._recover()

    recovered_ids: list[str] = []
    while not queue._queue.empty():
        recovered_ids.append(_queued_job_id(await queue._queue.get()))
    async with SessionLocal() as session:
        recovered = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
    active = [job for job in recovered if job.status == JobStatus.PAUSED]
    superseded = [job for job in recovered if job.status == JobStatus.FAILED]
    assert len(active) == 1
    assert recovered_ids == []
    assert len(superseded) == 2
    assert {(job.payload or {}).get("superseded_by_job_id") for job in superseded} == {active[0].id}


@pytest.mark.asyncio
async def test_source_maintenance_requeues_blocked_job_after_last_delete():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="source-maintenance",
            sag_source_config_id="source-maintenance-config"[:36],
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
        process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={
                "process_checkpoint": {"processed_chunk_ids": ["chunk-1"]},
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                },
            },
        )
        session.add(process)
        await session.commit()
        process_id, source_id = process.id, source.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    queue.begin_source_maintenance(source_id, "delete-1")
    queue.begin_source_maintenance(source_id, "delete-2")

    await queue.enqueue(process_id)
    assert queue._queue.empty()

    await queue.finish_source_maintenance(source_id, "delete-1")
    assert queue._queue.empty()

    await queue.finish_source_maintenance(source_id, "delete-2")
    queued = await queue._queue.get()
    assert _queued_job_id(queued) == process_id
    async with SessionLocal() as session:
        recovered = await session.get(Job, process_id)
        assert recovered.payload["process_checkpoint"]["processed_chunk_ids"] == ["chunk-1"]
        assert recovered.payload["resume_requested"] is True
        assert recovered.payload["_scheduler"] == {"priority": 10}


@pytest.mark.asyncio
async def test_source_maintenance_only_resumes_eligible_peer_documents():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="resume-filter", sag_source_config_id="resume-filter-config"[:36])
        session.add(source)
        await session.flush()
        extracting = Document(
            source_id=source.id,
            filename="extracting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/extracting.md",
            status=DocumentStatus.EXTRACTING,
        )
        paused = Document(
            source_id=source.id,
            filename="paused.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/paused.md",
            status=DocumentStatus.PAUSED,
        )
        failed_delete = Document(
            source_id=source.id,
            filename="failed-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed-delete.md",
            status=DocumentStatus.DELETE_FAILED,
        )
        session.add_all([extracting, paused, failed_delete])
        await session.flush()

        def blocked(document_id: str) -> Job:
            return Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.QUEUED,
                source_id=source.id,
                document_id=document_id,
                payload={
                    "_scheduler": {
                        "priority": 50,
                        "blocked_reason": "source_maintenance",
                    }
                },
            )

        resumable = blocked(extracting.id)
        user_paused = blocked(paused.id)
        delete_target = blocked(failed_delete.id)
        session.add_all([resumable, user_paused, delete_target])
        await session.commit()
        source_id = source.id
        resumable_id, paused_id, target_id = resumable.id, user_paused.id, delete_target.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    queue.begin_source_maintenance(source_id, "delete-1")
    await queue.finish_source_maintenance(source_id, "delete-1")

    queued_ids: list[str] = []
    while not queue._queue.empty():
        queued_ids.append(_queued_job_id(await queue._queue.get()))
    assert queued_ids == [resumable_id]
    async with SessionLocal() as session:
        resumed = await session.get(Job, resumable_id)
        still_paused = await session.get(Job, paused_id)
        still_target = await session.get(Job, target_id)
        assert resumed.payload["resume_requested"] is True
        assert still_paused.payload["_scheduler"]["blocked_reason"] == "source_maintenance"
        assert still_target.payload["_scheduler"]["blocked_reason"] == "source_maintenance"


@pytest.mark.asyncio
async def test_recovery_reconciles_orphaned_pausing_and_deleting_documents():
    import asyncio

    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="transition-recovery", sag_source_config_id="transition-recovery-config"[:36])
        session.add(source)
        await session.flush()
        pausing = Document(
            source_id=source.id,
            filename="pausing.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pausing.md",
            status=DocumentStatus.PAUSING,
        )
        deleting = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        session.add_all([pausing, deleting])
        await session.flush()
        interrupted_process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=pausing.id,
            payload={"pause_requested": True},
        )
        session.add(interrupted_process)
        await session.commit()
        pausing_id, deleting_id, process_id = pausing.id, deleting.id, interrupted_process.id

    class WaitingEngine:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.release = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.waiting.set()
            await self.release.wait()

        async def end_document_maintenance(self, *_args, **_kwargs):
            return None

    engine = WaitingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=engine, concurrency=1)
    await queue._recover()

    async with SessionLocal() as session:
        recovered_pause = await session.get(Document, pausing_id)
        recovered_delete = await session.get(Document, deleting_id)
        recovered_process = await session.get(Job, process_id)
        recovered_delete_job = await session.scalar(
            select(Job).where(
                Job.document_id == deleting_id,
                Job.type == JobType.DELETE_DOCUMENT,
            )
        )
        assert recovered_pause.status == DocumentStatus.PAUSED
        assert recovered_process.status == JobStatus.PAUSED
        assert recovered_delete.status == DocumentStatus.DELETING
        assert recovered_delete.error is None
        assert recovered_delete_job is not None
        assert recovered_delete_job.status == JobStatus.PAUSED
        assert recovered_delete_job.payload["target_document_id"] == deleting_id
    assert not engine.waiting.is_set()
    assert queue.source_maintenance_requested(recovered_delete.source_id) is False
    engine.release.set()
    await queue.stop()


@pytest.mark.asyncio
async def test_recovery_unblocks_source_job_when_delete_job_no_longer_exists():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="orphaned-maintenance",
            sag_source_config_id="orphaned-maintenance-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="orphaned.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/orphaned.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add(process)
        await session.commit()
        process_id = process.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._recover()

    recovered_ids: list[str] = []
    while not queue._queue.empty():
        recovered_ids.append(_queued_job_id(await queue._queue.get()))
    assert recovered_ids == []
    async with SessionLocal() as session:
        recovered = await session.get(Job, process_id)
        assert recovered.status == JobStatus.PAUSED
        assert recovered.payload["_scheduler"]["blocked_reason"] == "manual_recovery"
        assert not recovered.payload.get("resume_requested")


@pytest.mark.asyncio
async def test_new_process_job_is_blocked_while_source_maintenance_is_active():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="active-maintenance", sag_source_config_id="active-config"[:36])
        session.add(source)
        await session.flush()
        process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            payload={"resume_requested": True},
        )
        session.add(process)
        await session.commit()
        source_id, process_id = source.id, process.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    queue.begin_source_maintenance(source_id, "delete-job")
    await queue.enqueue(process_id)

    assert queue._queue.empty()
    async with SessionLocal() as session:
        blocked = await session.get(Job, process_id)
        assert blocked.payload["_scheduler"]["blocked_reason"] == "source_maintenance"


@pytest.mark.asyncio
async def test_user_can_resume_job_after_source_maintenance_has_finished(document_engine_manager):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="manual-resume", sag_source_config_id="manual-resume-config"[:36])
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="manual.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/manual.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.PAUSED,
            source_id=source.id,
            document_id=document.id,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add(job)
        await session.commit()

        queue = FakeQueue()
        resumed = await resume_document(
            session, source, document.id, job_queue=queue, engine_manager=document_engine_manager
        )

        assert resumed.status == JobStatus.QUEUED
        assert resumed.payload["_scheduler"] == {"priority": 10}
        assert queue.ids == [job.id]


@pytest.mark.asyncio
async def test_scheduler_yield_keeps_document_extracting(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.control import JobYielded
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class YieldingEngine(UncommittedDocumentManager):
        async def process_document(self, _config_id, _path, **kwargs):
            assert await kwargs["should_pause"]() is True
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="yield-source",
            sag_source_config_id="yield-source-config"[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="long.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/long.md",
            status=DocumentStatus.EXTRACTING,
            progress=52,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={
                "process_request": document_request(
                    source.sag_source_config_id, source_id="engine-document"
                ).model_dump(mode="json")
            },
        )
        session.add(job)
        await session.commit()
        source_id, document_id = source.id, document.id

        queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
        queue.begin_source_maintenance(source_id, "delete-job")
        from sag_api.jobs.tasks import process_document

        with pytest.raises(JobYielded):
            await process_document(
                session,
                job,
                engine_manager=YieldingEngine(),
                job_queue=queue,
            )

        await session.refresh(document)
        assert document.id == document_id
        assert document.status == DocumentStatus.EXTRACTING
        assert document.progress == 52


@pytest.mark.asyncio
async def test_delete_failure_releases_source_maintenance_and_retries_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class FailingDeleteEngine:
        async def delete_data(self, *_args, **_kwargs):
            raise RuntimeError("cleanup failed")

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="failed-delete", sag_source_config_id="failed-delete-config"[:36])
        session.add(source)
        await session.flush()
        deleting = Document(
            source_id=source.id,
            filename="delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/delete.md",
            status=DocumentStatus.DELETING,
            sag_source_id="engine-delete",
        )
        keep = Document(
            source_id=source.id,
            filename="keep.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/keep.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add_all([deleting, keep])
        await session.flush()
        delete_job = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=deleting.id,
        )
        blocked_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=keep.id,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add_all([delete_job, blocked_job])
        await session.commit()
        source_id = source.id
        deleting_id, delete_job_id, blocked_job_id = deleting.id, delete_job.id, blocked_job.id

    queue = InProcessAsyncQueue(SessionLocal, FailingDeleteEngine(), concurrency=1)
    queue.begin_source_maintenance(source_id, delete_job_id)
    await queue._run(delete_job_id)

    assert queue.source_maintenance_requested(source_id) is False
    async with SessionLocal() as session:
        deleting = await session.get(Document, deleting_id)
        blocked = await session.get(Job, blocked_job_id)
        delete_job = await session.get(Job, delete_job_id)
        assert deleting.status == DocumentStatus.DELETE_FAILED
        assert delete_job.status == JobStatus.FAILED
        assert blocked.payload["resume_requested"] is True
        assert blocked.payload["_scheduler"] == {"priority": 10}
    assert not queue._retry_tasks
    await queue.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_status", ["deleting", "delete_failed"])
@pytest.mark.usefixtures("controller_identity")
async def test_delete_control_state_late_receipt_does_not_advance_visible_progress(
    protected_status,
):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.tasks import process_document

    class LateReceiptEngine(UncommittedDocumentManager):
        async def process_document(self, _config_id, request_factory, **kwargs):
            request = await request_factory(None, *processing_parameters())
            await kwargs["on_stage"]("extracting")
            await kwargs["on_progress"](2, 2)
            return document_receipt(request, chunk_count=2, event_count=2, token_usage=500)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"late-checkpoint-{protected_status}",
            sag_source_config_id=(f"late-checkpoint-config-{protected_status}")[:36],
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="late.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/late.md",
            status=DocumentStatus(protected_status),
            progress=52,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            payload={
                "processing_identity": controller_processing_identity(document.storage_path),
                "process_request": document_request(source.sag_source_config_id, source_id="engine-delete").model_dump(
                    mode="json"
                ),
            },
        )
        session.add(job)
        await session.commit()

        with pytest.raises(JobPaused):
            await process_document(session, job, engine_manager=LateReceiptEngine())

        await session.refresh(document)
        await session.refresh(job)
        assert document.status == DocumentStatus(protected_status)
        assert document.progress == 52
        assert document.sag_source_id is None
        assert job.payload["process_request"]["source"]["descriptor"]["source_id"] == "engine-delete"
        assert "process_checkpoint" not in job.payload


@pytest.mark.asyncio
@pytest.mark.usefixtures("controller_identity")
async def test_deleting_active_document_keeps_its_queued_peer_outside_cleanup(tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services.document_service import create_document_from_upload, delete_document

    async def skip_unrelated_projection(*args, **kwargs):
        return []

    # This document-only manager cannot rebuild global users' Universe views.
    # Keep real document admission/leases/SQL; projection scheduling has its own tests.
    monkeypatch.setattr("sag_api.services.universe_service.schedule_universe_refresh", skip_unrelated_projection)

    class CoordinatedEngine(UncommittedDocumentManager):
        def __init__(self):
            self.started_titles: set[str] = set()
            self.returned_titles: set[str] = set()
            self.first_started = asyncio.Event()
            self.processors_idle = asyncio.Event()
            self.release_initial_batch = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.deleted: list[str] = []
            self.requests = {}

        async def process_document(self, config_id, request_factory, **kwargs):
            title = kwargs["document_title"]
            request = await request_factory(DocumentRequestBuilder(config_id), *processing_parameters())
            self.requests[title] = request
            await kwargs["on_stage"]("extracting")
            self.started_titles.add(title)
            self.first_started.set()
            await self.release_initial_batch.wait()
            self.returned_titles.add(title)
            self.processors_idle.set()
            if await kwargs["should_pause"]():
                return None
            return document_receipt(request, chunk_count=2, event_count=2, token_usage=200)

        async def begin_document_maintenance(self, *_args, **_kwargs):
            await self.processors_idle.wait()

        async def end_document_maintenance(self, *_args, **_kwargs):
            return None

        async def delete_data(self, request, *, source):
            from tests.document_requests import deletion_receipt

            assert source.sag_source_config_id
            self.deleted.append(request.source_id)
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            return deletion_receipt(request)

    await init_db()
    from uuid import uuid4

    source_id = uuid4().hex
    engine = CoordinatedEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    await queue.start()
    try:
        async with SessionLocal() as session:
            source = Source(id=source_id, name="concurrent-delete", sag_source_config_id="concurrent-config"[:36])
            session.add(source)
            await session.commit()
            document_a, process_a = await create_document_from_upload(
                session,
                source,
                filename="a.md",
                content_type="text/markdown",
                data=b"# A\n\ncontent",
                upload_dir=str(tmp_path),
                job_queue=queue,
            )
            # This scenario requires A already running before its queued peer
            # exists; serial admission does not promise which simultaneous worker wins.
            await asyncio.wait_for(engine.first_started.wait(), timeout=2)
            document_b, process_b = await create_document_from_upload(
                session,
                source,
                filename="b.md",
                content_type="text/markdown",
                data=b"# B\n\ncontent",
                upload_dir=str(tmp_path),
                job_queue=queue,
            )
            source_id, document_a_id, document_b_id = source.id, document_a.id, document_b.id
            process_a_id, process_b_id = process_a.id, process_b.id

        await asyncio.wait_for(engine.first_started.wait(), timeout=2)
        assert engine.started_titles == {"a"}
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            await delete_document(
                session,
                source,
                document_a_id,
                job_queue=queue,
            )
        engine.release_initial_batch.set()

        try:
            await asyncio.wait_for(engine.cleanup_started.wait(), timeout=2)
        except TimeoutError:
            async with SessionLocal() as session:
                process_a = await session.get(Job, process_a_id)
                process_b = await session.get(Job, process_b_id)
                document_a = await session.get(Document, document_a_id)
                document_b = await session.get(Document, document_b_id)
                jobs = list((await session.scalars(select(Job).where(Job.source_id == source_id))).all())
                pytest.fail(
                    "cleanup did not start; "
                    f"A=({process_a.status if process_a else None}, {process_a.error if process_a else None}, "
                    f"{document_a.status if document_a else None}, {document_a.error if document_a else None}) "
                    f"B=({process_b.status if process_b else None}, {process_b.error if process_b else None}, "
                    f"{document_b.status if document_b else None}, {document_b.error if document_b else None}) "
                    f"jobs={[(item.id, item.type, item.status, item.error) for item in jobs]}"
                )
        for _ in range(200):
            async with SessionLocal() as session:
                yielding_document = await session.get(Document, document_b_id)
                yielding_job = await session.scalar(select(Job).where(Job.document_id == document_b_id))
                if (
                    yielding_job.status == JobStatus.QUEUED
                    and (yielding_job.payload.get("_scheduler") or {}).get("blocked_reason") == "source_maintenance"
                ):
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("document B did not yield while document A was being deleted")
        assert yielding_document.status == DocumentStatus.PENDING
        assert yielding_job.payload["_scheduler"]["blocked_reason"] == "source_maintenance"

        engine.release_cleanup.set()
        for _ in range(200):
            async with SessionLocal() as session:
                deleted_document = await session.get(Document, document_a_id)
                resumed_document = await session.get(Document, document_b_id)
                resumed_job = await session.scalar(select(Job).where(Job.document_id == document_b_id))
                if (
                    deleted_document is None
                    and resumed_document.status == DocumentStatus.READY
                    and resumed_job.status == JobStatus.SUCCEEDED
                ):
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("document B did not resume after document A was deleted")

        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            resumed_job = await session.scalar(select(Job).where(Job.document_id == document_b_id))
            assert source.document_count == 1
            assert source.chunk_count == 2
            assert source.event_count == 2
            assert resumed_job.status == JobStatus.SUCCEEDED
            assert resumed_job.attempts == 1
        assert engine.deleted == [engine.requests["a"].source.descriptor.source_id]
    finally:
        engine.release_initial_batch.set()
        engine.release_cleanup.set()
        await queue.stop()
        # SQLite can retain a writer briefly while cancelled background
        # refresh jobs unwind. Retry test cleanup so it cannot pollute the
        # following Universe contract test with this synthetic source.
        from sqlalchemy.exc import OperationalError

        for attempt in range(5):
            try:
                async with SessionLocal() as session:
                    source = await session.get(Source, source_id)
                    if source is not None:
                        await session.delete(source)
                        await session.commit()
                break
            except OperationalError as error:
                if "database is locked" not in str(error).lower() or attempt == 4:
                    raise
                await asyncio.sleep(0.05 * (2**attempt))


@pytest.mark.asyncio
async def test_delete_admission_contention_keeps_maintenance_and_attempt_budget(tmp_path):
    """A transient admission owner must not let peer work bypass queued cleanup."""
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services.source_operation_service import acquire_operation_lease

    class CleanupEngine:
        async def begin_document_maintenance(self, *_args, **_kwargs):
            pass

        async def end_document_maintenance(self, *_args, **_kwargs):
            pass

        async def delete_data(self, request, **_kwargs):
            from tests.document_requests import deletion_receipt

            return deletion_receipt(request)

    await init_db()
    queue = InProcessAsyncQueue(SessionLocal, CleanupEngine(), concurrency=1)
    await queue.start()
    async with SessionLocal() as session:
        source = Source(name="delete-admission-contention", sag_source_config_id=tmp_path.name)
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleted.md",
            storage_path=str(tmp_path / "deleted.md"),
            status=DocumentStatus.DELETING,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(job)
        await session.commit()
        source_id, job_id, document_id = source.id, job.id, document.id
    try:
        async with acquire_operation_lease(
            SessionLocal,
            [f"source:{source_id}"],
            owner="processor-admission:peer",
        ):
            await queue.enqueue(job_id)

            async def yielded():
                async with SessionLocal() as session:
                    current = await session.get(Job, job_id)
                    return current.started_at is not None and current.status == JobStatus.QUEUED

            await _wait_until(yielded)
            async with SessionLocal() as session:
                current = await session.get(Job, job_id)
                current_document = await session.get(Document, document_id)
                assert current.attempts == 0
                assert current.error is None
                assert current_document.status == DocumentStatus.DELETING
                first_admission = current.started_at
            assert queue.source_maintenance_requested(source_id)

            async def admitted_again():
                async with SessionLocal() as session:
                    current = await session.get(Job, job_id)
                    return current.status == JobStatus.QUEUED and current.started_at != first_admission

            await _wait_until(admitted_again)
            async with SessionLocal() as session:
                current = await session.get(Job, job_id)
                assert current.attempts == 0 and current.error is None
            assert queue.source_maintenance_requested(source_id)
    finally:
        await queue.stop()
        async with SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
