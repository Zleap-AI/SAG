import asyncio

import pytest


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
async def test_new_maintenance_request_is_not_lost_while_previous_window_closes():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job, Source
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue

    class ClosingEngine:
        def __init__(self):
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

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
