"""Concurrency and transaction guards for aggregate universe rebuilds."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select


class RecordingQueue:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class StaticOverviewEngine:
    async def universe_overview_stats(self, _source_config_id: str, **_kwargs):
        from sag_api.sag.dto import UniverseSourceStatsInfo

        return UniverseSourceStatsInfo(event_count=3, entity_count=2, relation_count=4)


async def _create_user_and_source():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source, User

    await init_db()
    suffix = uuid.uuid4().hex
    async with SessionLocal() as session:
        user = User(
            email=f"universe-queue-{suffix}@t.com",
            password_hash="test-only",
            name="Universe Queue",
        )
        source = Source(
            name=f"Universe Source {suffix[:8]}",
            sag_source_config_id=f"src_{suffix[:16]}",
        )
        session.add_all([user, source])
        await session.commit()
        return user.id, source.id


@pytest.mark.asyncio
async def test_manual_rebuild_coalesces_one_queued_job():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.services.universe_service import enqueue_universe_rebuild

    user_id, _source_id = await _create_user_and_source()
    queue = RecordingQueue()
    async with SessionLocal() as session:
        first = await enqueue_universe_rebuild(session, queue, user_id=user_id)
        second = await enqueue_universe_rebuild(session, queue, user_id=user_id)
        assert first.id == second.id

        jobs = list(
            (
                await session.execute(
                    select(Job).where(
                        Job.type == JobType.INDEX_UNIVERSE,
                        Job.status == JobStatus.QUEUED,
                    )
                )
            ).scalars()
        )
        matching = [
            job for job in jobs if str((job.payload or {}).get("user_id") or "") == user_id
        ]
        assert [job.id for job in matching] == [first.id]

        # Once the first job is running, repeated requests coalesce into one
        # queued follow-up rather than starting a concurrent rebuild.
        first.status = JobStatus.RUNNING
        await session.commit()
        follow_up = await enqueue_universe_rebuild(session, queue, user_id=user_id)
        same_follow_up = await enqueue_universe_rebuild(session, queue, user_id=user_id)
        assert follow_up.id == same_follow_up.id
        assert follow_up.id != first.id
    assert queue.job_ids == [first.id, follow_up.id]


@pytest.mark.asyncio
async def test_rebuild_only_clears_captured_dirty_revision():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import UniverseDirtySource
    from sag_api.services.universe_service import rebuild_universe_overview

    user_id, source_id = await _create_user_and_source()
    async with SessionLocal() as session:
        dirty = UniverseDirtySource(
            user_id=user_id,
            source_id=source_id,
            reason="initial",
            revision=1,
        )
        session.add(dirty)
        await session.commit()
        dirty_id = dirty.id

    called = asyncio.Event()
    proceed = asyncio.Event()

    class BlockingEngine(StaticOverviewEngine):
        async def universe_overview_stats(self, source_config_id: str, **kwargs):
            called.set()
            await proceed.wait()
            return await super().universe_overview_stats(source_config_id, **kwargs)

    async def build():
        async with SessionLocal() as session:
            return await rebuild_universe_overview(session, BlockingEngine(), user_id)

    task = asyncio.create_task(build())
    await asyncio.wait_for(called.wait(), timeout=2)
    async with SessionLocal() as session:
        dirty = await session.get(UniverseDirtySource, dirty_id)
        assert dirty is not None
        dirty.revision += 1
        dirty.reason = "changed_during_build"
        dirty.updated_at = datetime.now(UTC)
        await session.commit()
    proceed.set()
    await asyncio.wait_for(task, timeout=5)

    async with SessionLocal() as session:
        dirty = await session.get(UniverseDirtySource, dirty_id)
        assert dirty is not None
        assert dirty.revision == 2


@pytest.mark.asyncio
async def test_rebuild_recomputes_only_dirty_source_and_reuses_clean_stats():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source, UniverseDirtySource, UniversePartition
    from sag_api.sag.dto import UniverseSourceStatsInfo
    from sag_api.services.universe_service import rebuild_universe_overview

    user_id, dirty_source_id = await _create_user_and_source()
    async with SessionLocal() as session:
        dirty_source = await session.get(Source, dirty_source_id)
        assert dirty_source is not None
        dirty_config_id = dirty_source.sag_source_config_id

    class CountingEngine:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def universe_overview_stats(self, source_config_id: str, **_kwargs):
            self.calls.append(source_config_id)
            return UniverseSourceStatsInfo(
                event_count=9 if source_config_id == dirty_config_id else 3,
                entity_count=2,
                relation_count=4,
            )

    engine = CountingEngine()
    async with SessionLocal() as session:
        first = await rebuild_universe_overview(session, engine, user_id)
        first_clean = list(
            (
                await session.execute(
                    select(UniversePartition).where(
                        UniversePartition.overview_id == first.id,
                        UniversePartition.source_id != dirty_source_id,
                        UniversePartition.kind == "source",
                    )
                )
            ).scalars()
        )
        session.add(
            UniverseDirtySource(
                user_id=user_id,
                source_id=dirty_source_id,
                reason="document_reprocessed",
                revision=1,
            )
        )
        await session.commit()

    engine.calls.clear()
    async with SessionLocal() as session:
        second = await rebuild_universe_overview(session, engine, user_id)
        second_clean = list(
            (
                await session.execute(
                    select(UniversePartition).where(
                        UniversePartition.overview_id == second.id,
                        UniversePartition.source_id != dirty_source_id,
                        UniversePartition.kind == "source",
                    )
                )
            ).scalars()
        )

    assert engine.calls == [dirty_config_id]
    first_by_source = {item.source_id: item for item in first_clean}
    second_by_source = {item.source_id: item for item in second_clean}
    assert first_by_source.keys() == second_by_source.keys()
    for source_id, previous in first_by_source.items():
        current = second_by_source[source_id]
        assert (current.event_count, current.entity_count, current.relation_count) == (
            previous.event_count,
            previous.entity_count,
            previous.relation_count,
        )
        assert (current.x, current.y, current.z) == (previous.x, previous.y, previous.z)


@pytest.mark.asyncio
async def test_source_stats_failure_keeps_previous_active_overview():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import UniverseOverview
    from sag_api.services.universe_service import active_overview, rebuild_universe_overview

    user_id, _source_id = await _create_user_and_source()
    async with SessionLocal() as session:
        previous = await rebuild_universe_overview(session, StaticOverviewEngine(), user_id)
        previous_id = previous.id

    class FailingEngine:
        async def universe_overview_stats(self, _source_config_id: str, **_kwargs):
            raise RuntimeError("statistics unavailable")

    async with SessionLocal() as session:
        with pytest.raises(RuntimeError, match="statistics unavailable"):
            await rebuild_universe_overview(session, FailingEngine(), user_id)

    async with SessionLocal() as session:
        active = await active_overview(session, user_id)
        assert active is not None
        assert active.id == previous_id
        failed = list(
            (
                await session.execute(
                    select(UniverseOverview).where(
                        UniverseOverview.user_id == user_id,
                        UniverseOverview.status == "failed",
                    )
                )
            ).scalars()
        )
        assert failed and failed[-1].is_active is False


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_invalidate_activated_overview(monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.services import universe_service

    user_id, _source_id = await _create_user_and_source()
    async with SessionLocal() as session:
        await universe_service.rebuild_universe_overview(
            session, StaticOverviewEngine(), user_id
        )

    async def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(universe_service, "_cleanup_old_overviews", fail_cleanup)
    async with SessionLocal() as session:
        activated = await universe_service.rebuild_universe_overview(
            session, StaticOverviewEngine(), user_id
        )
        activated_id = activated.id

    async with SessionLocal() as session:
        active = await universe_service.active_overview(session, user_id)
        assert active is not None
        assert active.id == activated_id
        assert active.status == "ready"
        assert active.is_active is True


@pytest.mark.asyncio
async def test_index_universe_jobs_are_serialized_per_user(monkeypatch):
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    user_id, _source_id = await _create_user_and_source()
    async with SessionLocal() as session:
        jobs = [
            Job(
                type=JobType.INDEX_UNIVERSE,
                status=JobStatus.QUEUED,
                payload={"user_id": user_id},
            )
            for _ in range(2)
        ]
        session.add_all(jobs)
        await session.commit()
        job_ids = [job.id for job in jobs]

    active_handlers = 0
    maximum_concurrency = 0

    async def handler(_session, _job, **_kwargs):
        nonlocal active_handlers, maximum_concurrency
        active_handlers += 1
        maximum_concurrency = max(maximum_concurrency, active_handlers)
        await asyncio.sleep(0.05)
        active_handlers -= 1

    monkeypatch.setitem(TASK_HANDLERS, JobType.INDEX_UNIVERSE, handler)
    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=2)
    await asyncio.gather(*(queue._run(job_id) for job_id in job_ids))
    assert maximum_concurrency == 1

    async with SessionLocal() as session:
        completed = [await session.get(Job, job_id) for job_id in job_ids]
        assert all(job is not None and job.status == JobStatus.SUCCEEDED for job in completed)
