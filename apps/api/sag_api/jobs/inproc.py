"""进程内 asyncio 任务队列 —— 随 API 进程起停。

- N 个 worker 协程从队列取 job_id，加载 Job，维护状态机并分发处理器。
- 启动时「恢复」上次残留的 QUEUED/RUNNING 任务（RUNNING 重置为 QUEUED 重跑）。
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from sag_api.core.config import settings
from sag_api.core.errors import ServiceUnavailableError, UpstreamError
from sag_api.core.logging import get_logger
from sag_api.db.models import Document
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.control import JobDeleted, JobPaused, JobYielded
from sag_api.jobs.queue import JobQueue
from sag_api.jobs.scheduling import (
    DELETE_PRIORITY,
    DELETE_WAITING_SOURCE,
    RESUME_PRIORITY,
    SOURCE_MAINTENANCE,
    get_blocked_reason,
    get_priority,
    set_scheduler,
)
from sag_api.jobs.tasks import TASK_HANDLERS
from sag_api.sag import EngineManager

log = get_logger("jobs")

# 退避基数（秒）：第 n 次重试等待 base**n。测试可 monkeypatch 缩短。
_BACKOFF_BASE_SECONDS = 2.0
_RECOVERY_LOCK_RETRIES = 4
_MAINTENANCE_CLOSE_RETRY_SECONDS = 0.5
_RETRY_ENQUEUE_RETRY_SECONDS = 0.5
_MAINTENANCE_JOB_TYPES = {
    JobType.DELETE_DOCUMENT,
    JobType.REPROCESS_DOCUMENT,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _is_retryable(exc: Exception) -> bool:
    """瞬时故障（限流/超时/上游暂不可用）可重试；输入/配置类错误不重试。"""
    return isinstance(
        exc,
        (ServiceUnavailableError, UpstreamError, OperationalError),
    )


async def _mark_document_waiting_retry(session, job) -> None:
    """Keep a retryable document active without discarding its checkpoint."""
    if job.type != JobType.PROCESS_DOCUMENT or not job.document_id:
        return
    document = await session.get(Document, job.document_id)
    if document is None:
        return
    document.status = DocumentStatus.PENDING
    document.error = None


async def _mark_reprocess_failed(session, document_id: str, message: str) -> None:
    """Record reprocess failure without reviving a concurrent delete."""
    await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status.not_in(
                [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]
            ),
        )
        .values(status=DocumentStatus.FAILED, error=message)
    )


async def _converge_document_paused(session, job) -> None:
    """Finish a won pause transition without overwriting a concurrent resume."""
    if job.type != JobType.PROCESS_DOCUMENT or not job.document_id:
        return
    await session.execute(
        update(Document)
        .where(
            Document.id == job.document_id,
            Document.status == DocumentStatus.PAUSING,
        )
        .values(status=DocumentStatus.PAUSED, error=None)
    )


class InProcessAsyncQueue(JobQueue):
    def __init__(
        self,
        session_factory: async_sessionmaker,
        engine_manager: EngineManager,
        *,
        concurrency: int = 2,
    ) -> None:
        self._session_factory = session_factory
        self._engine_manager = engine_manager
        self._concurrency = concurrency
        self._queue: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._enqueue_sequence = itertools.count()
        self._workers: list[asyncio.Task] = []
        self._retry_tasks: set[asyncio.Task] = set()
        self._universe_user_locks: dict[str, asyncio.Lock] = {}
        self._source_maintenance_jobs: dict[str, set[str]] = {}
        self._source_maintenance_tasks: dict[str, asyncio.Task] = {}
        self._source_maintenance_ready: set[str] = set()
        self._source_maintenance_dispatched: dict[str, str] = {}
        self._source_maintenance_closing: set[str] = set()
        self._started = False

    async def enqueue(self, job_id: str) -> None:
        from sag_api.db.models import Job

        for attempt in range(_RECOVERY_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    job = await session.get(Job, job_id)
                    if job is None:
                        return
                    if job.type in _MAINTENANCE_JOB_TYPES and job.source_id:
                        self.begin_source_maintenance(job.source_id, job.id)
                        job.payload = set_scheduler(
                            job.payload,
                            blocked_reason=DELETE_WAITING_SOURCE,
                        )
                        await session.commit()
                        source_id = job.source_id
                        if source_id in self._source_maintenance_ready:
                            await self._dispatch_next_maintenance(source_id)
                        else:
                            self._schedule_source_maintenance(source_id)
                        return
                    if (
                        job.type == JobType.PROCESS_DOCUMENT
                        and job.source_id
                        and self.source_maintenance_requested(job.source_id)
                    ):
                        job.payload = set_scheduler(
                            job.payload,
                            blocked_reason=SOURCE_MAINTENANCE,
                        )
                        await session.commit()
                        return
                    if get_blocked_reason(job.payload):
                        return
                    priority = get_priority(job.payload)
                await self._queue.put((priority, next(self._enqueue_sequence), job_id))
                return
            except OperationalError as error:
                locked = "database is locked" in str(error).lower()
                if not locked or attempt == _RECOVERY_LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(0.08 * (2**attempt))

    async def enqueue_durably(self, job_id: str) -> None:
        """Dispatch now when possible and supervise a retry after any failure."""
        try:
            await self.enqueue(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - durable row remains recoverable
            log.exception("持久化任务首次派发失败，转后台重排 job=%s", job_id)
            self._schedule_retry(job_id, 0.0)

    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        self._source_maintenance_jobs.setdefault(source_id, set()).add(job_id)

    def source_maintenance_requested(self, source_id: str) -> bool:
        return bool(self._source_maintenance_jobs.get(source_id))

    def _schedule_source_maintenance(self, source_id: str) -> None:
        current = self._source_maintenance_tasks.get(source_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._coordinate_source_maintenance(source_id),
            name=f"sag-source-maintenance-{source_id}",
        )
        self._source_maintenance_tasks[source_id] = task

        def discard(completed: asyncio.Task) -> None:
            if self._source_maintenance_tasks.get(source_id) is completed:
                self._source_maintenance_tasks.pop(source_id, None)

        task.add_done_callback(discard)

    async def _record_source_maintenance_window_failure(
        self,
        source_id: str,
        error: Exception,
    ) -> bool:
        """Bound maintenance admission failures and persist their final state."""
        from sag_api.db.models import Job

        registered_ids = set(self._source_maintenance_jobs.get(source_id, set()))
        if not registered_ids:
            return False

        message = getattr(error, "message", None) or str(error)
        retry_ids: set[str] = set()
        async with self._session_factory() as session:
            jobs = list(
                (
                    await session.scalars(
                        select(Job).where(
                            Job.id.in_(registered_ids),
                            Job.source_id == source_id,
                            Job.type.in_(_MAINTENANCE_JOB_TYPES),
                            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                        )
                    )
                ).all()
            )
            for job in jobs:
                job.attempts += 1
                if _is_retryable(error) and job.attempts < settings.job_max_attempts:
                    job.status = JobStatus.QUEUED
                    job.finished_at = None
                    job.error = (
                        f"第 {job.attempts} 次建立信源维护窗口失败，将重试：{message}"
                    )
                    retry_ids.add(job.id)
                    if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                        document = await session.get(Document, job.document_id)
                        if document is not None:
                            document.status = DocumentStatus.DELETING
                            document.error = None
                    continue

                job.status = JobStatus.FAILED
                job.error = message
                job.finished_at = _now()
                if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                    document = await session.get(Document, job.document_id)
                    if document is not None:
                        document.status = DocumentStatus.DELETE_FAILED
                        document.error = message
                elif job.type == JobType.REPROCESS_DOCUMENT and job.document_id:
                    await _mark_reprocess_failed(session, job.document_id, message)
            await session.commit()

        active = self._source_maintenance_jobs.get(source_id)
        if active is None:
            return False
        terminal_ids = registered_ids - retry_ids
        active.difference_update(terminal_ids)
        dispatched = self._source_maintenance_dispatched.get(source_id)
        if dispatched in terminal_ids:
            self._source_maintenance_dispatched.pop(source_id, None)
        return bool(active)

    async def _coordinate_source_maintenance(self, source_id: str) -> None:
        """Wait for a maintenance window without consuming a job worker."""
        from sag_api.db.models import Source

        delay = 0.5
        while self.source_maintenance_requested(source_id):
            window_open = False
            source = None
            source_config_id = ""
            try:
                async with self._session_factory() as session:
                    source = await session.get(Source, source_id)
                    if source is None:
                        return
                    source_config_id = source.sag_source_config_id
                await self._engine_manager.begin_document_maintenance(
                    source_config_id,
                    source=source,
                )
                window_open = True
                if not self.source_maintenance_requested(source_id):
                    await self._engine_manager.end_document_maintenance(
                        source_config_id,
                        source=source,
                    )
                    return
                self._source_maintenance_ready.add(source_id)
                await self._dispatch_next_maintenance(source_id)
                if (
                    source_id not in self._source_maintenance_ready
                    and source_id not in self._source_maintenance_closing
                    and self.source_maintenance_requested(source_id)
                ):
                    # A new control request arrived while this coordinator was
                    # closing an empty/ghost window. Reuse the tracked task to
                    # open the next window instead of spawning an untracked one.
                    continue
                return
            except asyncio.CancelledError:
                if window_open and source is not None:
                    try:
                        await self._engine_manager.end_document_maintenance(
                            source_config_id,
                            source=source,
                        )
                    except Exception:  # noqa: BLE001 - stop() retries ready windows
                        self._source_maintenance_ready.add(source_id)
                        log.exception(
                            "取消协调器时释放信源维护窗口失败 source=%s",
                            source_id,
                        )
                    else:
                        self._source_maintenance_ready.discard(source_id)
                else:
                    self._source_maintenance_ready.discard(source_id)
                raise
            except Exception as error:  # noqa: BLE001 - coordinator retries off-worker
                if window_open and source is not None:
                    log.exception("信源维护窗口内协调失败 source=%s", source_id)
                    self._source_maintenance_ready.add(source_id)
                    while source_id in self._source_maintenance_ready:
                        try:
                            await self._engine_manager.end_document_maintenance(
                                source_config_id,
                                source=source,
                            )
                            self._source_maintenance_ready.discard(source_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001 - retry before reopening
                            log.exception(
                                "重试释放信源维护窗口失败 source=%s",
                                source_id,
                            )
                            await asyncio.sleep(delay)
                else:
                    self._source_maintenance_ready.discard(source_id)

                try:
                    retry = await self._record_source_maintenance_window_failure(
                        source_id,
                        error,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - preserve fence until persisted
                    log.exception(
                        "持久化信源维护窗口失败状态异常，后台重试 source=%s",
                        source_id,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                if not retry:
                    await self._close_source_maintenance(source_id)
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _dispatch_next_maintenance(self, source_id: str) -> None:
        """Dispatch at most one maintenance job per source while its window stays open."""
        if (
            source_id not in self._source_maintenance_ready
            or source_id in self._source_maintenance_closing
        ):
            return
        from sag_api.db.models import Job

        active_ids = self._source_maintenance_jobs.get(source_id, set())
        if not active_ids:
            return
        registered_ids = set(active_ids)
        registered_dispatched = self._source_maintenance_dispatched.get(source_id)
        query_ids = set(registered_ids)
        if registered_dispatched is not None:
            query_ids.add(registered_dispatched)
        close_empty_window = False
        job_id: str | None = None
        priority = DELETE_PRIORITY
        async with self._session_factory() as session:
            live_jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .where(
                            Job.id.in_(query_ids),
                            Job.source_id == source_id,
                            Job.type.in_(_MAINTENANCE_JOB_TYPES),
                            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                        )
                        .order_by(Job.created_at, Job.id)
                    )
                ).all()
            )
            live_ids = {candidate.id for candidate in live_jobs}
            stale_ids = registered_ids - live_ids
            current_ids = self._source_maintenance_jobs.get(source_id)
            if current_ids is None:
                return
            # Only remove IDs from the snapshot queried above. A new maintenance
            # request may be registered while the database query is awaiting;
            # it must survive this reconciliation and open its own window.
            current_ids.difference_update(stale_ids)
            dispatched = self._source_maintenance_dispatched.get(source_id)
            if (
                dispatched is not None
                and dispatched == registered_dispatched
                and dispatched not in live_ids
            ):
                self._source_maintenance_dispatched.pop(source_id, None)
                dispatched = None
            if stale_ids:
                log.warning(
                    "清理失效的信源维护任务 source=%s jobs=%s",
                    source_id,
                    sorted(stale_ids),
                )
            if (
                source_id not in self._source_maintenance_ready
                or source_id in self._source_maintenance_closing
            ):
                return
            if not current_ids:
                close_empty_window = True
            else:
                running = next(
                    (
                        candidate
                        for candidate in live_jobs
                        if candidate.id in current_ids
                        and candidate.status == JobStatus.RUNNING
                    ),
                    None,
                )
                if running is not None:
                    self._source_maintenance_dispatched[source_id] = running.id
                    return
                if dispatched is not None:
                    return
                job = next(
                    (
                        candidate
                        for candidate in live_jobs
                        if candidate.id in current_ids
                        and candidate.status == JobStatus.QUEUED
                    ),
                    None,
                )
                if job is None:
                    return
                job.payload = set_scheduler(job.payload, blocked_reason=None)
                priority = get_priority(job.payload)
                await session.commit()
                job_id = job.id
        if close_empty_window:
            await self._close_source_maintenance(source_id)
            return
        if job_id is None:
            return
        self._source_maintenance_dispatched[source_id] = job_id
        await self._queue.put((priority, next(self._enqueue_sequence), job_id))

    async def _close_source_maintenance(self, source_id: str) -> None:
        if source_id in self._source_maintenance_closing:
            return
        self._source_maintenance_closing.add(source_id)

        from sag_api.db.models import Job, Source

        current_task = asyncio.current_task()
        coordinator = self._source_maintenance_tasks.get(source_id)
        closing_in_coordinator = coordinator is current_task
        try:
            if (
                coordinator is not None
                and not closing_in_coordinator
                and not coordinator.done()
            ):
                coordinator.cancel()
                await asyncio.gather(coordinator, return_exceptions=True)

            # First make every blocked PROCESS job durable. If engine release
            # then fails transiently, retain these IDs and retry only the engine
            # phase so no wake-up can be lost or duplicated.
            while True:
                candidate_ready_ids: set[str] = set()
                candidate_source = None
                try:
                    async with self._session_factory() as session:
                        candidate_source = await session.get(Source, source_id)
                        rows = list(
                            (
                                await session.scalars(
                                    select(Job).where(
                                        Job.source_id == source_id,
                                        Job.type == JobType.PROCESS_DOCUMENT,
                                        Job.status == JobStatus.QUEUED,
                                    ).join(
                                        Document,
                                        Job.document_id == Document.id,
                                    ).where(
                                        Document.status.in_(
                                            [
                                                DocumentStatus.PENDING,
                                                DocumentStatus.LOADING,
                                                DocumentStatus.EXTRACTING,
                                            ]
                                        )
                                    )
                                )
                            ).all()
                        )
                        for blocked in rows:
                            if (
                                get_blocked_reason(blocked.payload)
                                != SOURCE_MAINTENANCE
                            ):
                                continue
                            payload = set_scheduler(
                                blocked.payload,
                                priority=RESUME_PRIORITY,
                                blocked_reason=None,
                            )
                            payload["resume_requested"] = True
                            blocked.payload = payload
                            candidate_ready_ids.add(blocked.id)
                        await session.commit()
                    source = candidate_source
                    ready_ids = candidate_ready_ids
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - close must remain retryable
                    log.exception(
                        "持久化信源维护唤醒失败，后台重试 source=%s",
                        source_id,
                    )
                    await asyncio.sleep(_MAINTENANCE_CLOSE_RETRY_SECONDS)

            # Persist wake-ups before reopening engine admission. A restart can
            # recover this durable state even if cancellation happens before the
            # in-memory enqueue below.
            while source_id in self._source_maintenance_ready and source is not None:
                try:
                    await self._engine_manager.end_document_maintenance(
                        source.sag_source_config_id,
                        source=source,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the window tracked and retry
                    log.exception(
                        "释放信源维护窗口失败，后台重试 source=%s",
                        source_id,
                    )
                    await asyncio.sleep(_MAINTENANCE_CLOSE_RETRY_SECONDS)

            self._source_maintenance_ready.discard(source_id)
            self._source_maintenance_closing.discard(source_id)
            if self.source_maintenance_requested(source_id):
                # A new control request can arrive while the old window closes.
                # The current coordinator continues its loop; a worker-owned
                # close starts a fresh coordinator after the old one is gone.
                if not closing_in_coordinator:
                    self._schedule_source_maintenance(source_id)
            else:
                self._source_maintenance_jobs.pop(source_id, None)
            for ready_id in ready_ids:
                await self.enqueue_durably(ready_id)
        finally:
            # Never leave a source permanently fenced if database or engine
            # cleanup is cancelled during shutdown.
            self._source_maintenance_closing.discard(source_id)

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        active = self._source_maintenance_jobs.get(source_id)
        if active is not None:
            active.discard(job_id)
        if self._source_maintenance_dispatched.get(source_id) == job_id:
            self._source_maintenance_dispatched.pop(source_id, None)
        if active:
            if source_id in self._source_maintenance_ready:
                await self._dispatch_next_maintenance(source_id)
            else:
                self._schedule_source_maintenance(source_id)
            return
        await self._close_source_maintenance(source_id)

    def _schedule_retry(self, job_id: str, delay: float) -> None:
        """退避后重新入队（不阻塞 worker）。"""

        async def _later() -> None:
            try:
                await asyncio.sleep(delay)
                retry_delay = _RETRY_ENQUEUE_RETRY_SECONDS
                while True:
                    try:
                        await self.enqueue(job_id)
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - durable row needs dispatch
                        log.exception("延迟重排入队失败，后台重试 job=%s", job_id)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(max(retry_delay * 2, 0.0), 60.0)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(_later(), name=f"sag-retry-{job_id}")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            # Recover before workers begin consuming so a failed startup cannot
            # leave detached workers holding database sessions.
            await self._recover()
            for i in range(self._concurrency):
                self._workers.append(
                    asyncio.create_task(self._worker_loop(i), name=f"sag-worker-{i}")
                )
        except BaseException:
            await self.stop()
            raise
        log.info("任务队列已启动（并发=%d）", self._concurrency)

    async def stop(self) -> None:
        # Stop every task that can mutate maintenance state before reading that
        # state. In particular, a delete worker may finish maintenance while
        # shutdown is awaiting a database lookup; iterating the live set there
        # used to raise ``Set changed size during iteration`` and abort the rest
        # of application cleanup.
        maintenance_tasks = list(self._source_maintenance_tasks.values())
        for task in maintenance_tasks:
            task.cancel()
        retry_tasks = list(self._retry_tasks)
        for t in retry_tasks:
            t.cancel()
        workers = list(self._workers)
        for worker in workers:
            worker.cancel()

        if maintenance_tasks:
            await asyncio.gather(*maintenance_tasks, return_exceptions=True)
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._source_maintenance_tasks.clear()
        self._retry_tasks.clear()
        self._workers.clear()

        ready_source_ids = tuple(self._source_maintenance_ready)
        if ready_source_ids:
            from sag_api.db.models import Source

            sources = {}
            try:
                async with self._session_factory() as session:
                    sources = {
                        source_id: await session.get(Source, source_id)
                        for source_id in ready_source_ids
                    }
            except Exception:  # noqa: BLE001 - shutdown must continue closing resources
                log.exception("停机读取信源维护窗口失败，继续关闭队列")
            for source_id, source in sources.items():
                if source is None:
                    continue
                try:
                    await self._engine_manager.end_document_maintenance(
                        source.sag_source_config_id,
                        source=source,
                    )
                except Exception:  # noqa: BLE001 - one source must not abort shutdown
                    log.exception("停机释放信源维护窗口失败 source=%s", source_id)
        self._universe_user_locks.clear()
        self._source_maintenance_jobs.clear()
        self._source_maintenance_ready.clear()
        self._source_maintenance_dispatched.clear()
        self._source_maintenance_closing.clear()
        self._started = False

    async def _recover(self) -> None:
        from sag_api.db.models import Job, Source

        rows = []
        for attempt in range(_RECOVERY_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    rows = (
                        await session.execute(
                            select(Job).where(
                                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
                            )
                        )
                    ).scalars().all()

                    # Older versions could persist multiple active jobs for one
                    # document when identical control requests raced. Keep the
                    # most advanced job of each type and make the rest inert
                    # before workers are allowed to recover them.
                    active_by_document: dict[tuple[str, JobType], list] = {}
                    for candidate in rows:
                        target_id = candidate.document_id or str(
                            (candidate.payload or {}).get("target_document_id") or ""
                        )
                        if target_id and candidate.type in {
                            JobType.PROCESS_DOCUMENT,
                            JobType.REPROCESS_DOCUMENT,
                            JobType.DELETE_DOCUMENT,
                        }:
                            active_by_document.setdefault(
                                (target_id, candidate.type), []
                            ).append(candidate)
                    for candidates in active_by_document.values():
                        if len(candidates) < 2:
                            continue

                        def recovery_rank(candidate):
                            checkpoint = (candidate.payload or {}).get(
                                "process_checkpoint"
                            )
                            processed_count = len(
                                checkpoint.get("processed_chunk_ids", [])
                            ) if isinstance(checkpoint, dict) else 0
                            return (
                                candidate.status == JobStatus.RUNNING,
                                processed_count,
                                candidate.progress,
                                candidate.created_at,
                                candidate.id,
                            )

                        keeper = max(candidates, key=recovery_rank)
                        for duplicate in candidates:
                            if duplicate.id == keeper.id:
                                continue
                            duplicate.status = JobStatus.FAILED
                            duplicate.error = "任务已被恢复流程中的较新进度取代"
                            duplicate.finished_at = _now()
                            duplicate.payload = {
                                **(duplicate.payload or {}),
                                "superseded_by_job_id": keeper.id,
                            }
                    for job in rows:
                        if (
                            job.type in _MAINTENANCE_JOB_TYPES
                            and job.source_id
                            and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                        ):
                            self.begin_source_maintenance(job.source_id, job.id)
                    for job in rows:
                        if job.status == JobStatus.RUNNING:
                            job.status = JobStatus.QUEUED

                    transition_documents = list(
                        (
                            await session.scalars(
                                select(Document).where(
                                    Document.status.in_(
                                        [
                                            DocumentStatus.PAUSING,
                                            DocumentStatus.DELETING,
                                            DocumentStatus.DELETE_FAILED,
                                        ]
                                    )
                                )
                            )
                        ).all()
                    )
                    jobs_by_document: dict[str, list] = {}
                    transition_source_ids: set[str] = set()
                    for candidate in rows:
                        target_id = candidate.document_id or str(
                            (candidate.payload or {}).get("target_document_id") or ""
                        )
                        if target_id:
                            jobs_by_document.setdefault(target_id, []).append(candidate)
                    for document in transition_documents:
                        transition_source_ids.add(document.source_id)
                        document_jobs = jobs_by_document.get(document.id, [])
                        if document.status == DocumentStatus.PAUSING:
                            document.status = DocumentStatus.PAUSED
                            document.error = None
                            for process_job in document_jobs:
                                if process_job.type != JobType.PROCESS_DOCUMENT:
                                    continue
                                process_job.status = JobStatus.PAUSED
                                payload = dict(process_job.payload or {})
                                payload.pop("pause_requested", None)
                                process_job.payload = payload
                        else:
                            document.status = DocumentStatus.DELETING
                            document.error = None
                            for process_job in document_jobs:
                                if process_job.type == JobType.PROCESS_DOCUMENT:
                                    process_job.status = JobStatus.PAUSED
                            has_delete_job = any(
                                candidate.type == JobType.DELETE_DOCUMENT
                                and candidate.status == JobStatus.QUEUED
                                for candidate in document_jobs
                            )
                            if not has_delete_job:
                                delete_job = Job(
                                    type=JobType.DELETE_DOCUMENT,
                                    source_id=document.source_id,
                                    document_id=document.id,
                                    status=JobStatus.QUEUED,
                                    payload=set_scheduler(
                                        {"target_document_id": document.id},
                                        priority=DELETE_PRIORITY,
                                    ),
                                )
                                session.add(delete_job)
                                await session.flush()
                                rows.append(delete_job)
                                self.begin_source_maintenance(
                                    document.source_id,
                                    delete_job.id,
                                )

                    if transition_source_ids:
                        from sag_api.services.document_service import _refresh_source_counts

                        for transition_source_id in transition_source_ids:
                            transition_source = await session.get(Source, transition_source_id)
                            if transition_source is not None:
                                await _refresh_source_counts(session, transition_source)

                    for job in rows:
                        if (
                            job.source_id
                            and get_blocked_reason(job.payload) == SOURCE_MAINTENANCE
                            and not self.source_maintenance_requested(job.source_id)
                        ):
                            document = (
                                await session.get(Document, job.document_id)
                                if job.document_id
                                else None
                            )
                            if document is None or document.status not in {
                                DocumentStatus.PENDING,
                                DocumentStatus.LOADING,
                                DocumentStatus.EXTRACTING,
                            }:
                                continue
                            payload = set_scheduler(
                                job.payload,
                                priority=RESUME_PRIORITY,
                                blocked_reason=None,
                            )
                            payload["resume_requested"] = True
                            job.payload = payload
                    await session.commit()
                break
            except OperationalError as error:
                locked = "database is locked" in str(error).lower()
                if not locked or attempt == _RECOVERY_LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(0.08 * (2**attempt))
        rows = [
            job
            for job in rows
            if job.status == JobStatus.QUEUED
            and (
                job.type in _MAINTENANCE_JOB_TYPES
                or not get_blocked_reason(job.payload)
            )
        ]
        rows.sort(key=lambda value: (value.created_at, value.id))
        for job in rows:
            await self.enqueue(job.id)
        if rows:
            log.info("恢复 %d 个未完成任务", len(rows))

    async def _worker_loop(self, idx: int) -> None:
        while True:
            _priority, _sequence, job_id = await self._queue.get()
            running = asyncio.create_task(self._run(job_id), name=f"sag-job-{job_id}")
            try:
                await running
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
                log.info("worker#%d 已取消 job=%s", idx, job_id)
            except Exception:  # noqa: BLE001
                log.exception("worker#%d 处理 job=%s 异常", idx, job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        from sag_api.db.models import Job

        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            # 队列里可能残留暂停前的 job_id，也可能被重复 enqueue；只有 QUEUED
            # 才能启动，避免同一任务被两个 worker 同时执行。
            if job is None or job.status != JobStatus.QUEUED:
                return
            universe_user_id = (
                str((job.payload or {}).get("user_id") or "")
                if job.type == JobType.INDEX_UNIVERSE
                else ""
            )

        if universe_user_id:
            lock = self._universe_user_locks.setdefault(universe_user_id, asyncio.Lock())
            async with lock:
                await self._run_job(job_id)
            return
        await self._run_job(job_id)

    async def _run_job(self, job_id: str) -> None:
        from sag_api.db.models import Job

        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return
            payload = dict(job.payload or {})
            is_resume = bool(payload.pop("resume_requested", False))
            claim = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(
                    payload=payload,
                    status=JobStatus.RUNNING,
                    started_at=_now(),
                    finished_at=None,
                    attempts=job.attempts if is_resume else job.attempts + 1,
                    progress=job.progress if is_resume else max(job.progress, 0.05),
                    error=None,
                )
            )
            await session.commit()
            if claim.rowcount != 1:
                return
            await session.refresh(job)
            claimed_started_at = job.started_at

            handler = TASK_HANDLERS.get(job.type)
            if handler is None:
                await session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING,
                        Job.started_at == claimed_started_at,
                    )
                    .values(
                        status=JobStatus.FAILED,
                        error=f"未知任务类型：{job.type}",
                        finished_at=_now(),
                    )
                )
                await session.commit()
                return

            claimed_type = job.type
            claimed_source_id = job.source_id
            release_source_maintenance = False
            retry_source_maintenance = False
            retry_delay_to_schedule: float | None = None

            try:
                await handler(session, job, engine_manager=self._engine_manager, job_queue=self)
                succeeded = await session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING,
                        Job.started_at == claimed_started_at,
                    )
                    .values(
                        status=JobStatus.SUCCEEDED,
                        progress=1.0,
                        finished_at=_now(),
                        error=None,
                    )
                )
                release_source_maintenance = bool(
                    succeeded.rowcount
                    and claimed_type in _MAINTENANCE_JOB_TYPES
                )
            except asyncio.CancelledError:
                raise
            except JobDeleted:
                if claimed_type in _MAINTENANCE_JOB_TYPES and claimed_source_id:
                    await self.finish_source_maintenance(claimed_source_id, job_id)
                return
            except JobYielded as yielded:
                await session.rollback()
                job = await session.get(Job, job_id)
                if (
                    job is not None
                    and job.status == JobStatus.PAUSED
                    and job.started_at == claimed_started_at
                ):
                    await _converge_document_paused(session, job)
                    log.info("任务已被并发暂停，忽略让行信号 job=%s", job_id)
                elif job is not None:
                    payload = set_scheduler(
                        job.payload,
                        blocked_reason=yielded.reason,
                    )
                    yielded_update = await session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status == JobStatus.RUNNING,
                            Job.started_at == claimed_started_at,
                        )
                        .values(
                            payload=payload,
                            status=JobStatus.QUEUED,
                            finished_at=None,
                            error=None,
                        )
                    )
                    if yielded_update.rowcount:
                        log.info(
                            "任务临时让行 job=%s reason=%s",
                            job_id,
                            yielded.reason,
                        )
            except JobPaused:
                await session.rollback()
                job = await session.get(Job, job_id)
                if job is not None:
                    paused = await session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.status.in_([JobStatus.RUNNING, JobStatus.PAUSED]),
                            Job.started_at == claimed_started_at,
                        )
                        .values(
                            status=JobStatus.PAUSED,
                            finished_at=None,
                            error=None,
                        )
                    )
                    if paused.rowcount:
                        await _converge_document_paused(session, job)
                        log.info(
                            "任务已暂停 job=%s progress=%.0f%%",
                            job_id,
                            job.progress * 100,
                        )
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                job = await session.get(Job, job_id)
                if (
                    job is not None
                    and job.status == JobStatus.PAUSED
                    and job.started_at == claimed_started_at
                ):
                    await _converge_document_paused(session, job)
                    log.info("任务已被并发暂停，忽略 handler 失败 job=%s", job_id)
                    await session.commit()
                    return
                msg = getattr(e, "message", None) or str(e)
                attempts = job.attempts if job is not None else settings.job_max_attempts
                delete_cleanup = bool(
                    job is not None and job.type == JobType.DELETE_DOCUMENT
                )
                retry = job is not None and (
                    _is_retryable(e)
                    and attempts < settings.job_max_attempts
                )
                if job is not None and job.status == JobStatus.RUNNING:
                    if retry:
                        # 退避重排：状态回 QUEUED，延迟 base**attempts 秒后重新入队
                        delay = min(_BACKOFF_BASE_SECONDS ** min(attempts, 10), 60.0)
                        retry_update = await session.execute(
                            update(Job)
                            .where(
                                Job.id == job_id,
                                Job.status == JobStatus.RUNNING,
                                Job.started_at == claimed_started_at,
                            )
                            .values(
                                status=JobStatus.QUEUED,
                                progress=0.0,
                                error=f"第 {attempts} 次失败，将重试：{msg}",
                            )
                        )
                        if retry_update.rowcount:
                            await _mark_document_waiting_retry(session, job)
                            if delete_cleanup and job.document_id:
                                document = await session.get(Document, job.document_id)
                                if document is not None:
                                    document.status = DocumentStatus.DELETING
                                    document.error = None
                            retry_delay_to_schedule = delay
                            retry_source_maintenance = (
                                claimed_type in _MAINTENANCE_JOB_TYPES
                            )
                            release_source_maintenance = bool(
                                delete_cleanup and claimed_source_id
                            )
                            log.warning(
                                "任务可重试 job=%s（第 %d/%d 次），%.1fs 后重排：%s",
                                job_id,
                                attempts,
                                settings.job_max_attempts,
                                delay,
                                msg,
                            )
                    else:
                        failed_update = await session.execute(
                            update(Job)
                            .where(
                                Job.id == job_id,
                                Job.status == JobStatus.RUNNING,
                                Job.started_at == claimed_started_at,
                            )
                            .values(
                                status=JobStatus.FAILED,
                                error=msg,
                                finished_at=_now(),
                            )
                        )
                        if failed_update.rowcount:
                            if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                                document = await session.get(Document, job.document_id)
                                if document is not None:
                                    document.status = DocumentStatus.DELETE_FAILED
                                    document.error = msg
                                release_source_maintenance = bool(claimed_source_id)
                            elif (
                                job.type == JobType.REPROCESS_DOCUMENT
                                and job.document_id
                            ):
                                await _mark_reprocess_failed(
                                    session,
                                    job.document_id,
                                    msg,
                                )
                                release_source_maintenance = bool(claimed_source_id)
                            log.warning(
                                "任务失败 job=%s（尝试 %d 次）：%s",
                                job_id,
                                attempts,
                                msg,
                            )
            await session.commit()
            if retry_delay_to_schedule is not None:
                self._schedule_retry(job_id, retry_delay_to_schedule)
            if retry_source_maintenance and claimed_source_id:
                if self._source_maintenance_dispatched.get(claimed_source_id) == job_id:
                    self._source_maintenance_dispatched.pop(claimed_source_id, None)
            if release_source_maintenance and claimed_source_id:
                await self.finish_source_maintenance(claimed_source_id, job_id)
