import asyncio
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_pending_fast_delete_falls_back_when_process_is_claimed_mid_request(
    monkeypatch,
    tmp_path,
):
    from datetime import UTC, datetime

    from sqlalchemy import select, update

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class QueueSpy:
        def __init__(self):
            self.maintenance: list[tuple[str, str]] = []
            self.enqueued: list[str] = []

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

        async def enqueue(self, job_id: str):
            self.enqueued.append(job_id)

    await init_db()
    path = tmp_path / "claimed-during-delete.pdf"
    path.write_bytes(b"%PDF-1.4")
    async with SessionLocal() as session:
        source = Source(
            name="pending-fast-delete-claim-race",
            sag_source_config_id=f"pending-fast-delete-{uuid4().hex}",
            document_count=1,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename=path.name,
            content_type="application/pdf",
            size_bytes=path.stat().st_size,
            storage_path=str(path),
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        real_scalars = session.scalars
        scalars_calls = 0

        async def scalars_then_claim(statement, *args, **kwargs):
            nonlocal scalars_calls
            scalars_calls += 1
            if scalars_calls == 3:
                await session.commit()
                async with SessionLocal() as worker_session:
                    await worker_session.execute(
                        update(Job)
                        .where(
                            Job.id == process_job_id,
                            Job.status == JobStatus.QUEUED,
                        )
                        .values(
                            status=JobStatus.RUNNING,
                            started_at=datetime.now(UTC),
                            attempts=1,
                        )
                    )
                    await worker_session.commit()
            return await real_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", scalars_then_claim)
        queue = QueueSpy()
        delete_job = await delete_document(
            session,
            source,
            document_id,
            job_queue=queue,
        )

    async with SessionLocal() as session:
        deleting_document = await session.get(Document, document_id)
        stopped_process = await session.get(Job, process_job_id)
        delete_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.source_id == source_id,
                        Job.type == JobType.DELETE_DOCUMENT,
                    )
                )
            ).all()
        )
        assert deleting_document is not None
        assert deleting_document.status == DocumentStatus.DELETING
        assert stopped_process is not None
        assert stopped_process.status == JobStatus.RUNNING
        assert stopped_process.payload["pause_requested"] is True
        assert delete_job.status == JobStatus.QUEUED
        assert [candidate.id for candidate in delete_jobs] == [delete_job.id]
        assert queue.maintenance == [(source_id, delete_job.id)]
        assert queue.enqueued == [delete_job.id]
    assert path.exists()


@pytest.mark.asyncio
async def test_permanent_maintenance_window_failure_becomes_delete_failed():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class BrokenEngine:
        async def begin_document_maintenance(self, *_args, **_kwargs):
            raise ValueError("invalid maintenance configuration")

        async def end_document_maintenance(self, *_args, **_kwargs):
            raise AssertionError("a window that never opened must not be closed")

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="broken-maintenance-window",
            sag_source_config_id=f"broken-window-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/delete.md",
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
        source_id, document_id, job_id = source.id, document.id, job.id

    queue = InProcessAsyncQueue(SessionLocal, BrokenEngine(), concurrency=0)
    queue.begin_source_maintenance(source_id, job_id)
    queue._schedule_source_maintenance(source_id)
    coordinator = queue._source_maintenance_tasks[source_id]
    try:
        await asyncio.wait_for(asyncio.shield(coordinator), timeout=1)
        async with SessionLocal() as session:
            failed_job = await session.get(Job, job_id)
            failed_document = await session.get(Document, document_id)
            assert failed_job.status == JobStatus.FAILED
            assert failed_job.attempts == 1
            assert failed_document.status == DocumentStatus.DELETE_FAILED
            assert failed_document.error == "invalid maintenance configuration"
        assert queue.source_maintenance_requested(source_id) is False
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_retryable_maintenance_window_failure_is_bounded_and_retried():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class FlakyEngine:
        def __init__(self):
            self.begin_count = 0

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.begin_count += 1
            if self.begin_count == 1:
                raise ServiceUnavailableError("temporary maintenance outage")

        async def end_document_maintenance(self, *_args, **_kwargs):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="flaky-maintenance-window",
            sag_source_config_id=f"flaky-window-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/delete.md",
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
        source_id, document_id, job_id = source.id, document.id, job.id

    engine = FlakyEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=0)
    queue.begin_source_maintenance(source_id, job_id)
    queue._schedule_source_maintenance(source_id)
    coordinator = queue._source_maintenance_tasks[source_id]
    try:
        await asyncio.wait_for(asyncio.shield(coordinator), timeout=2)
        async with SessionLocal() as session:
            retried_job = await session.get(Job, job_id)
            deleting_document = await session.get(Document, document_id)
            assert retried_job.status == JobStatus.QUEUED
            assert retried_job.attempts == 1
            assert deleting_document.status == DocumentStatus.DELETING
        assert engine.begin_count == 2
        assert queue._source_maintenance_dispatched[source_id] == job_id
        assert (await asyncio.wait_for(queue._queue.get(), timeout=1))[-1] == job_id
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_never_started_pending_document_is_deleted_without_using_a_worker(tmp_path):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class QueueSpy:
        def begin_source_maintenance(self, *_args):
            raise AssertionError("metadata-only deletion must not open source maintenance")

        async def enqueue(self, _job_id):
            raise AssertionError("metadata-only deletion must not wait for a worker")

    await init_db()
    path = tmp_path / "pending.md"
    path.write_text("# pending", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(
            name="pending-fast-delete",
            sag_source_config_id="pending-fast-delete-config",
            document_count=1,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pending.md",
            content_type="text/markdown",
            size_bytes=9,
            storage_path=str(path),
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(process)
        await session.commit()
        source_id, document_id = source.id, document.id

        completed = await delete_document(
            session,
            source,
            document_id,
            job_queue=QueueSpy(),
        )

        assert completed.type == JobType.DELETE_DOCUMENT
        assert completed.status == JobStatus.SUCCEEDED
        assert completed.document_id is None
        assert completed.payload["target_document_id"] == document_id
        assert await session.get(Document, document_id) is None
        refreshed_source = await session.get(Source, source_id)
        assert refreshed_source.document_count == 0
    assert not path.exists()


@pytest.mark.asyncio
async def test_concurrent_pending_deletes_share_one_completed_job(tmp_path):
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class QueueSpy:
        def begin_source_maintenance(self, *_args):
            raise AssertionError("metadata-only deletion must not open source maintenance")

        async def enqueue(self, _job_id):
            raise AssertionError("metadata-only deletion must not wait for a worker")

    await init_db()
    path = tmp_path / "concurrent-pending.md"
    path.write_text("# pending", encoding="utf-8")
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-pending-delete",
            sag_source_config_id="concurrent-pending-delete-config",
            document_count=1,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pending.md",
            content_type="text/markdown",
            size_bytes=9,
            storage_path=str(path),
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.QUEUED,
                source_id=source.id,
                document_id=document.id,
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    async def remove():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await delete_document(
                session,
                source,
                document_id,
                job_queue=QueueSpy(),
            )

    first, second = await asyncio.gather(remove(), remove())

    async with SessionLocal() as session:
        completed = [
            job
            for job in (
                await session.scalars(
                    select(Job).where(
                        Job.source_id == source_id,
                        Job.type == JobType.DELETE_DOCUMENT,
                        Job.status == JobStatus.SUCCEEDED,
                    )
                )
            ).all()
            if (job.payload or {}).get("target_document_id") == document_id
        ]
        assert await session.get(Document, document_id) is None
    assert first.id == second.id
    assert [job.id for job in completed] == [first.id]
    assert not path.exists()


@pytest.mark.asyncio
async def test_delete_during_loading_is_cooperative_and_does_not_hard_cancel():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class QueueSpy:
        def __init__(self):
            self.maintenance: list[tuple[str, str]] = []
            self.enqueued: list[str] = []

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

        def cancel_running_job(self, _job_id: str):
            raise AssertionError("loading may have untracked engine data and must not be hard-cancelled")

        async def enqueue(self, job_id: str):
            self.enqueued.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="loading-delete",
            sag_source_config_id="loading-delete-config",
            document_count=1,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="loading.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/loading.md",
            status=DocumentStatus.LOADING,
            sag_source_id=None,
        )
        session.add(document)
        await session.flush()
        process = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
        )
        session.add(process)
        await session.commit()

        queue = QueueSpy()
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(process)
        await session.refresh(document)
        assert process.status == JobStatus.RUNNING
        assert process.payload["pause_requested"] is True
        assert document.status == DocumentStatus.DELETING
        await session.refresh(source)
        assert source.document_count == 0
        assert queue.maintenance == [(source.id, delete_job.id)]
        assert queue.enqueued == [delete_job.id]


@pytest.mark.asyncio
async def test_delete_waits_for_source_idle_outside_the_worker_queue():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import DELETE_WAITING_SOURCE, get_blocked_reason

    class CoordinatedEngine:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.idle = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.waiting.set()
            await self.idle.wait()

        async def end_document_maintenance(self, *_args, **_kwargs):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="park-delete", sag_source_config_id="park-delete-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="derived.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/derived.md",
            status=DocumentStatus.DELETING,
            sag_source_id="engine-derived",
        )
        session.add(document)
        await session.flush()
        delete_job = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"target_document_id": document.id},
        )
        normal_job = Job(type=JobType.SYNC_SOURCE, status=JobStatus.QUEUED)
        session.add_all([delete_job, normal_job])
        await session.commit()
        source_id, delete_id, normal_id = source.id, delete_job.id, normal_job.id

    engine = CoordinatedEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, delete_id)
    await queue.enqueue(delete_id)
    await asyncio.wait_for(engine.waiting.wait(), timeout=1)

    async with SessionLocal() as session:
        parked = await session.get(Job, delete_id)
        assert parked.status == JobStatus.QUEUED
        assert get_blocked_reason(parked.payload) == DELETE_WAITING_SOURCE
    assert queue._queue.empty()

    await queue.enqueue(normal_id)
    queued_normal = await queue._queue.get()
    assert queued_normal[-1] == normal_id
    queue._queue.task_done()

    engine.idle.set()
    queued_delete = await asyncio.wait_for(queue._queue.get(), timeout=1)
    assert queued_delete[-1] == delete_id
    queue._queue.task_done()
    await queue.stop()


@pytest.mark.asyncio
async def test_reprocess_cleanup_waits_for_source_idle_outside_the_worker_queue():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import DELETE_WAITING_SOURCE, get_blocked_reason

    class CoordinatedEngine:
        def __init__(self):
            self.waiting = asyncio.Event()
            self.idle = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.waiting.set()
            await self.idle.wait()

        async def end_document_maintenance(self, *_args, **_kwargs):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="park-reprocess", sag_source_config_id="park-reprocess-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        cleanup_job = Job(
            type=JobType.REPROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"target_document_id": document.id, "derived_source_ids": ["engine-old"]},
        )
        session.add(cleanup_job)
        await session.commit()
        source_id, cleanup_id = source.id, cleanup_job.id

    engine = CoordinatedEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, cleanup_id)
    await queue.enqueue(cleanup_id)
    await asyncio.wait_for(engine.waiting.wait(), timeout=1)

    async with SessionLocal() as session:
        parked = await session.get(Job, cleanup_id)
        assert parked.status == JobStatus.QUEUED
        assert get_blocked_reason(parked.payload) == DELETE_WAITING_SOURCE
    assert queue._queue.empty()

    engine.idle.set()
    queued_cleanup = await asyncio.wait_for(queue._queue.get(), timeout=1)
    assert queued_cleanup[-1] == cleanup_id
    queue._queue.task_done()
    await queue.stop()


@pytest.mark.asyncio
async def test_maintenance_release_happens_after_peer_resume_is_durable():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE, get_blocked_reason

    class OrderingEngine:
        released = False

        async def end_document_maintenance(self, *_args, **_kwargs):
            async with SessionLocal() as session:
                peer = await session.get(Job, peer_job_id)
                assert peer is not None
                assert get_blocked_reason(peer.payload) is None
                assert peer.payload["resume_requested"] is True
            self.released = True

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="release-order", sag_source_config_id="release-order-config")
        session.add(source)
        await session.flush()
        peer_document = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(peer_document)
        await session.flush()
        peer_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=peer_document.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add(peer_job)
        await session.commit()
        source_id, peer_job_id = source.id, peer_job.id

    engine = OrderingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "delete-completed")
    queue._source_maintenance_ready.add(source_id)

    await queue.finish_source_maintenance(source_id, "delete-completed")

    assert engine.released is True
    assert (await queue._queue.get())[-1] == peer_job_id


@pytest.mark.asyncio
async def test_cascaded_reprocess_job_does_not_leave_source_maintenance_stuck():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE, get_blocked_reason

    class ReleasingEngine:
        def __init__(self):
            self.end_count = 0

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="cascaded-reprocess-maintenance",
            sag_source_config_id="cascaded-reprocess-maintenance-config",
        )
        session.add(source)
        await session.flush()
        delete_target = Document(
            source_id=source.id,
            filename="delete-target.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/delete-target.md",
            status=DocumentStatus.DELETING,
        )
        peer = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add_all([delete_target, peer])
        await session.flush()
        reprocess = Job(
            type=JobType.REPROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=delete_target.id,
        )
        blocked_peer = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=peer.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add_all([reprocess, blocked_peer])
        await session.commit()
        source_id = source.id
        reprocess_id = reprocess.id
        peer_job_id = blocked_peer.id

    engine = ReleasingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, reprocess_id)
    queue.begin_source_maintenance(source_id, "finishing-delete")
    queue._source_maintenance_ready.add(source_id)
    queue._source_maintenance_dispatched[source_id] = "finishing-delete"

    async with SessionLocal() as session:
        target = await session.get(Document, delete_target.id)
        await session.delete(target)
        await session.commit()
        assert await session.get(Job, reprocess_id) is None

    await queue.finish_source_maintenance(source_id, "finishing-delete")

    assert queue.source_maintenance_requested(source_id) is False
    assert engine.end_count == 1
    assert (await queue._queue.get())[-1] == peer_job_id
    async with SessionLocal() as session:
        resumed = await session.get(Job, peer_job_id)
        assert get_blocked_reason(resumed.payload) is None
        assert resumed.payload["resume_requested"] is True


@pytest.mark.asyncio
async def test_coordinator_closes_window_when_only_maintenance_job_disappears():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE, get_blocked_reason

    class ImmediateEngine:
        def __init__(self):
            self.begin_count = 0
            self.end_count = 0

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.begin_count += 1

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="missing-only-maintenance",
            sag_source_config_id="missing-only-maintenance-config",
        )
        session.add(source)
        await session.flush()
        peer = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(peer)
        await session.flush()
        blocked_peer = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=peer.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add(blocked_peer)
        await session.commit()
        source_id = source.id
        peer_job_id = blocked_peer.id

    engine = ImmediateEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "missing-reprocess-job")
    queue._source_maintenance_dispatched[source_id] = "missing-reprocess-job"
    queue._schedule_source_maintenance(source_id)
    coordinator = queue._source_maintenance_tasks[source_id]

    await asyncio.wait_for(asyncio.shield(coordinator), timeout=1)

    assert coordinator.cancelled() is False
    assert queue.source_maintenance_requested(source_id) is False
    assert source_id not in queue._source_maintenance_dispatched
    assert engine.begin_count == 1
    assert engine.end_count == 1
    assert (await queue._queue.get())[-1] == peer_job_id
    async with SessionLocal() as session:
        resumed = await session.get(Job, peer_job_id)
        assert get_blocked_reason(resumed.payload) is None
        assert resumed.payload["resume_requested"] is True


@pytest.mark.asyncio
async def test_dispatch_restores_missing_running_maintenance_registration():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import DELETE_WAITING_SOURCE

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="restore-running-maintenance",
            sag_source_config_id="restore-running-maintenance-config",
        )
        session.add(source)
        await session.flush()
        running = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            payload={"target_document_id": "running-target"},
        )
        queued = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            payload={
                "target_document_id": "queued-target",
                "_scheduler": {"blocked_reason": DELETE_WAITING_SOURCE},
            },
        )
        session.add_all([running, queued])
        await session.commit()
        source_id = source.id
        running_id = running.id
        queued_id = queued.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    queue.begin_source_maintenance(source_id, running_id)
    queue.begin_source_maintenance(source_id, queued_id)
    queue._source_maintenance_ready.add(source_id)

    await queue._dispatch_next_maintenance(source_id)

    assert queue._source_maintenance_dispatched[source_id] == running_id
    assert queue._queue.empty()
    async with SessionLocal() as session:
        still_queued = await session.get(Job, queued_id)
        assert still_queued.payload["_scheduler"]["blocked_reason"] == DELETE_WAITING_SOURCE


@pytest.mark.asyncio
async def test_same_source_deletes_share_one_maintenance_window_and_run_serially(
    monkeypatch,
):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class CoordinatedEngine:
        def __init__(self):
            self.begin_count = 0
            self.end_count = 0
            self.active_deletes = 0
            self.max_active_deletes = 0
            self.deleted: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.begin_count += 1

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1

        async def delete_document_data(self, _config_id, document_source_id, **_kwargs):
            self.active_deletes += 1
            self.max_active_deletes = max(self.max_active_deletes, self.active_deletes)
            self.deleted.append(document_source_id)
            try:
                if len(self.deleted) == 1:
                    self.first_started.set()
                    await self.release_first.wait()
            finally:
                self.active_deletes -= 1

    async def no_universe_refresh(*_args, **_kwargs):
        return None

    import sag_api.services.universe_service as universe_service

    monkeypatch.setattr(universe_service, "schedule_universe_refresh", no_universe_refresh)
    await init_db()
    async with SessionLocal() as session:
        source = Source(name="batch-delete", sag_source_config_id="batch-delete-config")
        session.add(source)
        await session.flush()
        documents = [
            Document(
                source_id=source.id,
                filename=f"{name}.md",
                content_type="text/markdown",
                size_bytes=10,
                storage_path=f"/tmp/{name}.md",
                status=DocumentStatus.DELETING,
                sag_source_id=f"engine-{name}",
            )
            for name in ("a", "b")
        ]
        session.add_all(documents)
        await session.flush()
        jobs = [
            Job(
                type=JobType.DELETE_DOCUMENT,
                status=JobStatus.QUEUED,
                source_id=source.id,
                document_id=document.id,
                payload={"target_document_id": document.id},
            )
            for document in documents
        ]
        session.add_all(jobs)
        await session.commit()
        source_id = source.id
        document_ids = [document.id for document in documents]
        job_ids = [job.id for job in jobs]

    engine = CoordinatedEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=2)
    await queue.start()
    try:
        await asyncio.wait_for(engine.first_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert len(engine.deleted) == 1
        assert engine.deleted[0] in {"engine-a", "engine-b"}
        assert engine.max_active_deletes == 1

        engine.release_first.set()

        async def both_finished():
            async with SessionLocal() as session:
                rows = [await session.get(Document, document_id) for document_id in document_ids]
                return all(document is None for document in rows)

        async def wait_for_both():
            while not await both_finished():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_both(), timeout=2)
        async def maintenance_released():
            while engine.end_count != 1:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(maintenance_released(), timeout=1)
        async with SessionLocal() as session:
            completed = [await session.get(Job, job_id) for job_id in job_ids]
            assert all(job is not None and job.status == JobStatus.SUCCEEDED for job in completed)
        assert set(engine.deleted) == {"engine-a", "engine-b"}
        assert engine.begin_count == 1
        assert engine.end_count == 1
        assert engine.max_active_deletes == 1
        assert queue.source_maintenance_requested(source_id) is False
    finally:
        engine.release_first.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_workers_before_releasing_maintenance_windows():
    from types import SimpleNamespace

    from sag_api.jobs.inproc import InProcessAsyncQueue

    worker_started = asyncio.Event()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, source_id):
            # Reading maintenance sources while a worker can still mutate the
            # ready set caused the GitHub Actions shutdown race.
            assert worker.done()
            return SimpleNamespace(
                id=source_id,
                sag_source_config_id=f"config-{source_id}",
            )

    class Engine:
        def __init__(self):
            self.released: list[tuple[str, str]] = []

        async def end_document_maintenance(self, source_config_id, *, source):
            self.released.append((source_config_id, source.id))

    engine = Engine()
    queue = InProcessAsyncQueue(lambda: Session(), engine, concurrency=1)
    queue._source_maintenance_ready.update({"source-a", "source-b"})

    async def finishing_worker():
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            queue._source_maintenance_ready.discard("source-b")

    worker = asyncio.create_task(finishing_worker())
    queue._workers.append(worker)
    await worker_started.wait()

    try:
        await queue.stop()
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    assert engine.released == [("config-source-a", "source-a")]
    assert queue._source_maintenance_ready == set()


@pytest.mark.asyncio
async def test_new_maintenance_request_is_dispatched_after_previous_window_closes():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class ClosingEngine:
        def __init__(self):
            self.begin_count = 0
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            self.begin_count += 1

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_started.set()
            await self.release_end.wait()

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="closing-race", sag_source_config_id="closing-race-config")
        session.add(source)
        await session.flush()
        next_job = Job(
            type=JobType.DELETE_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            payload={"target_document_id": "next-document"},
        )
        session.add(next_job)
        await session.commit()
        source_id, next_job_id = source.id, next_job.id

    engine = ClosingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "finishing-job")
    queue._source_maintenance_ready.add(source_id)
    queue._source_maintenance_dispatched[source_id] = "finishing-job"

    finishing = asyncio.create_task(
        queue.finish_source_maintenance(source_id, "finishing-job")
    )
    await asyncio.wait_for(engine.end_started.wait(), timeout=1)
    queue.begin_source_maintenance(source_id, next_job_id)
    await queue.enqueue(next_job_id)
    engine.release_end.set()
    await asyncio.wait_for(finishing, timeout=1)

    assert queue.source_maintenance_requested(source_id) is True
    assert next_job_id in queue._source_maintenance_jobs[source_id]
    assert (await asyncio.wait_for(queue._queue.get(), timeout=1))[-1] == next_job_id
    assert queue._source_maintenance_dispatched[source_id] == next_job_id
    assert engine.begin_count == 1
    await queue.stop()


@pytest.mark.asyncio
async def test_closing_maintenance_window_retries_transient_engine_failure(
    monkeypatch,
):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs import inproc
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE, get_blocked_reason

    class FlakyEngine:
        def __init__(self):
            self.end_count = 0

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1
            if self.end_count == 1:
                raise RuntimeError("temporary engine release failure")

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="transient-maintenance-close",
            sag_source_config_id="transient-maintenance-close-config",
        )
        session.add(source)
        await session.flush()
        peer = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(peer)
        await session.flush()
        blocked_peer = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=peer.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add(blocked_peer)
        await session.commit()
        source_id, peer_job_id = source.id, blocked_peer.id

    monkeypatch.setattr(inproc, "_MAINTENANCE_CLOSE_RETRY_SECONDS", 0, raising=False)
    engine = FlakyEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "finishing-delete")
    queue._source_maintenance_ready.add(source_id)
    queue._source_maintenance_dispatched[source_id] = "finishing-delete"

    await asyncio.wait_for(
        queue.finish_source_maintenance(source_id, "finishing-delete"),
        timeout=1,
    )

    assert engine.end_count == 2
    assert queue.source_maintenance_requested(source_id) is False
    assert source_id not in queue._source_maintenance_ready
    assert source_id not in queue._source_maintenance_closing
    assert (await asyncio.wait_for(queue._queue.get(), timeout=1))[-1] == peer_job_id
    assert queue._queue.empty()
    async with SessionLocal() as session:
        resumed = await session.get(Job, peer_job_id)
        assert get_blocked_reason(resumed.payload) is None
        assert resumed.payload["resume_requested"] is True


@pytest.mark.asyncio
async def test_stop_tracks_coordinator_while_it_closes_a_ghost_window():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class BlockingEngine:
        def __init__(self):
            self.end_started = asyncio.Event()
            self.end_count = 0

        async def begin_document_maintenance(self, *_args, **_kwargs):
            return None

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1
            if self.end_count == 1:
                self.end_started.set()
                await asyncio.Event().wait()

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="tracked-ghost-close",
            sag_source_config_id="tracked-ghost-close-config",
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    engine = BlockingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "missing-maintenance-job")
    queue._schedule_source_maintenance(source_id)
    coordinator = queue._source_maintenance_tasks[source_id]

    await asyncio.wait_for(engine.end_started.wait(), timeout=1)
    assert queue._source_maintenance_tasks[source_id] is coordinator

    await asyncio.wait_for(queue.stop(), timeout=1)

    assert coordinator.done()
    assert engine.end_count == 2
    assert queue._source_maintenance_tasks == {}
    assert queue._source_maintenance_jobs == {}
    assert queue._source_maintenance_ready == set()


@pytest.mark.asyncio
async def test_maintenance_close_supervises_peer_enqueue_failure():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE, get_blocked_reason

    class ReleasingEngine:
        released = False

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.released = True

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="durable-peer-release",
            sag_source_config_id="durable-peer-release-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        peer = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=document.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add(peer)
        await session.commit()
        source_id, peer_job_id = source.id, peer.id

    engine = ReleasingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "delete-completed")
    queue._source_maintenance_ready.add(source_id)
    scheduled: list[tuple[str, float]] = []

    async def fail_enqueue(_job_id: str) -> None:
        raise RuntimeError("transient enqueue failure")

    queue.enqueue = fail_enqueue  # type: ignore[method-assign]
    queue._schedule_retry = lambda job_id, delay: scheduled.append(  # type: ignore[method-assign]
        (job_id, delay)
    )

    await queue.finish_source_maintenance(source_id, "delete-completed")

    assert engine.released is True
    assert scheduled == [(peer_job_id, 0.0)]
    async with SessionLocal() as session:
        resumed = await session.get(Job, peer_job_id)
        assert resumed.status == JobStatus.QUEUED
        assert get_blocked_reason(resumed.payload) is None
        assert resumed.payload["resume_requested"] is True
    assert queue._source_maintenance_closing == set()


@pytest.mark.asyncio
async def test_concurrent_finish_calls_close_and_resume_once():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.scheduling import SOURCE_MAINTENANCE

    class CountingEngine:
        def __init__(self):
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()
            self.end_count = 0

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1
            self.end_started.set()
            await self.release_end.wait()

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-maintenance-finish",
            sag_source_config_id="concurrent-maintenance-finish-config",
        )
        session.add(source)
        await session.flush()
        peer = Document(
            source_id=source.id,
            filename="peer.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/peer.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(peer)
        await session.flush()
        blocked_peer = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=peer.id,
            payload={"_scheduler": {"blocked_reason": SOURCE_MAINTENANCE}},
        )
        session.add(blocked_peer)
        await session.commit()
        source_id, peer_job_id = source.id, blocked_peer.id

    engine = CountingEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, "finishing-delete")
    queue._source_maintenance_ready.add(source_id)
    queue._source_maintenance_dispatched[source_id] = "finishing-delete"

    first = asyncio.create_task(
        queue.finish_source_maintenance(source_id, "finishing-delete")
    )
    await asyncio.wait_for(engine.end_started.wait(), timeout=1)
    second = asyncio.create_task(
        queue.finish_source_maintenance(source_id, "finishing-delete")
    )
    await asyncio.wait_for(second, timeout=1)
    engine.release_end.set()
    await asyncio.wait_for(first, timeout=1)

    assert engine.end_count == 1
    assert (await asyncio.wait_for(queue._queue.get(), timeout=1))[-1] == peer_job_id
    assert queue._queue.empty()
    assert queue.source_maintenance_requested(source_id) is False
    assert source_id not in queue._source_maintenance_ready
    assert source_id not in queue._source_maintenance_closing


@pytest.mark.asyncio
async def test_stop_releases_window_when_dispatch_and_release_both_fail():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class FlakyReleaseEngine:
        def __init__(self):
            self.end_count = 0
            self.second_end_started = asyncio.Event()

        async def begin_document_maintenance(self, *_args, **_kwargs):
            return None

        async def end_document_maintenance(self, *_args, **_kwargs):
            self.end_count += 1
            if self.end_count == 1:
                raise RuntimeError("temporary release failure")
            if self.end_count == 2:
                self.second_end_started.set()
                await asyncio.Event().wait()

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="dispatch-and-release-failure",
            sag_source_config_id="dispatch-and-release-failure-config",
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    engine = FlakyReleaseEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)

    async def fail_dispatch(_source_id):
        raise RuntimeError("temporary dispatch database failure")

    queue._dispatch_next_maintenance = fail_dispatch
    queue.begin_source_maintenance(source_id, "maintenance-job")
    queue._schedule_source_maintenance(source_id)

    await asyncio.wait_for(engine.second_end_started.wait(), timeout=2)
    assert source_id in queue._source_maintenance_ready

    await asyncio.wait_for(queue.stop(), timeout=1)

    # First release failed, the second was cancelled by stop(), and stop's
    # ready-window fallback performed the final successful release.
    assert engine.end_count == 3
    assert queue._source_maintenance_tasks == {}
    assert queue._source_maintenance_jobs == {}
    assert queue._source_maintenance_ready == set()
