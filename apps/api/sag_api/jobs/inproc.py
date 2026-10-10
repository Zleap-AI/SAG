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
from sag_api.core.errors import DocumentRestartRequiredError, ServiceUnavailableError, UpstreamError
from sag_api.core.logging import get_logger
from sag_api.db.models import Document, Source
from sag_api.db.models.job import job_payload_matches
from sag_api.enums import DocumentStatus, JobStatus, JobType, SourceStatus
from sag_api.jobs.control import JobDeleted, JobPaused, JobYielded
from sag_api.jobs.queue import JobQueue
from sag_api.jobs.scheduling import (
    DELETE_PRIORITY,
    DELETE_WAITING_SOURCE,
    MANUAL_RECOVERY,
    RESUME_PRIORITY,
    SOURCE_BUSY,
    SOURCE_MAINTENANCE,
    get_blocked_reason,
    get_priority,
    set_scheduler,
)
from sag_api.jobs.source_maintenance import SourceMaintenanceState
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
    JobType.DELETE_SOURCE,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _is_retryable(exc: Exception) -> bool:
    """瞬时故障（限流/超时/上游暂不可用）可重试；输入/配置类错误不重试。"""
    # 显式实例标记优先于异常类型兜底，避免 retryable=False 的
    # UpstreamError 仍被 isinstance 分支误判为可重试。
    explicit_retryable = getattr(exc, "__dict__", {}).get("retryable")
    if isinstance(explicit_retryable, bool):
        return explicit_retryable
    return bool(getattr(exc, "retryable", False)) or isinstance(
        exc,
        (ServiceUnavailableError, UpstreamError, OperationalError),
    )


async def _mark_document_waiting_retry(session, job) -> bool:
    """Keep a retryable document active without discarding its checkpoint."""
    if job.type != JobType.PROCESS_DOCUMENT or not job.document_id:
        return True
    marked = await session.execute(
        update(Document)
        .where(
            Document.id == job.document_id,
            Document.control_revision == job.payload["control_revision"],
            Document.status.in_([
                DocumentStatus.PENDING, DocumentStatus.LOADING,
                DocumentStatus.EXTRACTING, DocumentStatus.FAILED,
            ]),
        )
        .values(status=DocumentStatus.PENDING, processing_stage="waiting_retry", processing_run_id=None, error=None)
    )
    return marked.rowcount == 1


async def _mark_document_queued(session, job) -> None:
    """A released worker no longer owns a live processing snapshot."""
    if job.type != JobType.PROCESS_DOCUMENT or not job.document_id:
        return
    await session.execute(
        update(Document)
        .where(
            Document.id == job.document_id,
            Document.control_revision == job.payload["control_revision"],
            Document.status.in_([DocumentStatus.PENDING, DocumentStatus.LOADING, DocumentStatus.EXTRACTING]),
        )
        .values(status=DocumentStatus.PENDING, processing_stage="queued", processing_run_id=None)
    )


async def _converge_document_paused(session, job) -> None:
    """Finish a won pause transition without overwriting a concurrent resume."""
    if job.type != JobType.PROCESS_DOCUMENT or not job.document_id:
        return
    await session.execute(
        update(Document)
        .where(
            Document.id == job.document_id,
            Document.control_revision == job.payload["control_revision"],
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
        self._maintenance = SourceMaintenanceState()
        self._started = False

    # --- 维护窗口状态的兼容访问器 ---
    # 状态本身已移入 SourceMaintenanceState（见 sag_api/jobs/source_maintenance.py）；
    # 这里保留原有字段名，让既有调用点与测试继续按原方式读写。

    @property
    def _source_maintenance_jobs(self) -> dict[str, set[str]]:
        return self._maintenance.jobs

    @property
    def _source_maintenance_tasks(self) -> dict[str, asyncio.Task]:
        return self._maintenance.tasks

    @property
    def _source_maintenance_ready(self) -> set[str]:
        return self._maintenance.ready

    @property
    def _source_maintenance_dispatched(self) -> dict[str, str]:
        return self._maintenance.dispatched

    @property
    def _source_maintenance_closing(self) -> set[str]:
        return self._maintenance.closing

    async def enqueue(self, job_id: str) -> None:
        from sag_api.db.models import Job

        for attempt in range(_RECOVERY_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    job = await session.get(Job, job_id)
                    if job is None:
                        return
                    if get_blocked_reason(job.payload) == MANUAL_RECOVERY:
                        return
                    if job.type == JobType.PROCESS_DOCUMENT and job.source_id:
                        source = await session.get(Source, job.source_id, populate_existing=True)
                        if source is None or source.is_deleting:
                            job.status = JobStatus.PAUSED
                            job.payload = set_scheduler(job.payload, blocked_reason=SOURCE_MAINTENANCE)
                            await session.commit()
                            return
                    if job.type in _MAINTENANCE_JOB_TYPES and job.source_id:
                        self.begin_source_maintenance(job.source_id, job.id)
                        job.payload = set_scheduler(
                            job.payload,
                            blocked_reason=DELETE_WAITING_SOURCE,
                        )
                        await session.commit()
                        source_id = job.source_id
                        if source_id in self._maintenance.ready:
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
        self._maintenance.jobs.setdefault(source_id, set()).add(job_id)

    def source_maintenance_requested(self, source_id: str) -> bool:
        return bool(self._maintenance.jobs.get(source_id))

    def request_source_stop(self, source_id: str) -> None:
        self._maintenance.stop_requested.add(source_id)

    def clear_source_stop(self, source_id: str) -> None:
        self._maintenance.stop_requested.discard(source_id)

    def source_stop_requested(self, source_id: str) -> bool:
        return source_id in self._maintenance.stop_requested

    def _schedule_source_maintenance(self, source_id: str) -> None:
        current = self._maintenance.tasks.get(source_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._coordinate_source_maintenance(source_id),
            name=f"sag-source-maintenance-{source_id}",
        )
        self._maintenance.tasks[source_id] = task

        def discard(completed: asyncio.Task) -> None:
            if self._maintenance.tasks.get(source_id) is completed:
                self._maintenance.tasks.pop(source_id, None)

        task.add_done_callback(discard)

    async def _record_source_maintenance_window_failure(
        self,
        source_id: str,
        error: Exception,
    ) -> bool:
        """Bound maintenance admission failures and persist their final state."""
        from sag_api.db.models import Job

        registered_ids = set(self._maintenance.jobs.get(source_id, set()))
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
                    job.error = f"第 {job.attempts} 次建立信源维护窗口失败，将重试：{message}"
                    retry_ids.add(job.id)
                    if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                        document = await session.get(Document, job.document_id)
                        if document is not None:
                            document.status = DocumentStatus.DELETING
                            document.error = None
                    if job.type == JobType.DELETE_SOURCE:
                        source = await session.get(Source, source_id)
                        if source is not None:
                            source.status = SourceStatus.DELETING
                    continue

                job.status = JobStatus.FAILED
                job.error = message
                job.finished_at = _now()
                if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                    document = await session.get(Document, job.document_id)
                    if document is not None:
                        document.status = DocumentStatus.DELETE_FAILED
                        document.error = message
                if job.type == JobType.DELETE_SOURCE:
                    source = await session.get(Source, source_id)
                    if source is not None:
                        source.status = SourceStatus.DELETE_FAILED
            await session.commit()

        active = self._maintenance.jobs.get(source_id)
        if active is None:
            return False
        terminal_ids = registered_ids - retry_ids
        active.difference_update(terminal_ids)
        dispatched = self._maintenance.dispatched.get(source_id)
        if dispatched in terminal_ids:
            self._maintenance.dispatched.pop(source_id, None)
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
                self._maintenance.ready.add(source_id)
                await self._dispatch_next_maintenance(source_id)
                if (
                    source_id not in self._maintenance.ready
                    and source_id not in self._maintenance.closing
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
                        self._maintenance.ready.add(source_id)
                        log.exception(
                            "取消协调器时释放信源维护窗口失败 source=%s",
                            source_id,
                        )
                    else:
                        self._maintenance.ready.discard(source_id)
                else:
                    self._maintenance.ready.discard(source_id)
                raise
            except Exception as error:  # noqa: BLE001 - coordinator retries off-worker
                if window_open and source is not None:
                    log.exception("信源维护窗口内协调失败 source=%s", source_id)
                    self._maintenance.ready.add(source_id)
                    while source_id in self._maintenance.ready:
                        try:
                            await self._engine_manager.end_document_maintenance(
                                source_config_id,
                                source=source,
                            )
                            self._maintenance.ready.discard(source_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001 - retry before reopening
                            log.exception(
                                "重试释放信源维护窗口失败 source=%s",
                                source_id,
                            )
                            await asyncio.sleep(delay)
                else:
                    self._maintenance.ready.discard(source_id)

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
        if source_id not in self._maintenance.ready or source_id in self._maintenance.closing:
            return
        from sag_api.db.models import Job

        active_ids = self._maintenance.jobs.get(source_id, set())
        if not active_ids:
            return
        registered_ids = set(active_ids)
        registered_dispatched = self._maintenance.dispatched.get(source_id)
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
            current_ids = self._maintenance.jobs.get(source_id)
            if current_ids is None:
                return
            # Only remove IDs from the snapshot queried above. A new maintenance
            # request may be registered while the database query is awaiting;
            # it must survive this reconciliation and open its own window.
            current_ids.difference_update(stale_ids)
            dispatched = self._maintenance.dispatched.get(source_id)
            if dispatched is not None and dispatched == registered_dispatched and dispatched not in live_ids:
                self._maintenance.dispatched.pop(source_id, None)
                dispatched = None
            if stale_ids:
                log.warning(
                    "清理失效的信源维护任务 source=%s jobs=%s",
                    source_id,
                    sorted(stale_ids),
                )
            if source_id not in self._maintenance.ready or source_id in self._maintenance.closing:
                return
            if not current_ids:
                close_empty_window = True
            else:
                running = next(
                    (
                        candidate
                        for candidate in live_jobs
                        if candidate.id in current_ids and candidate.status == JobStatus.RUNNING
                    ),
                    None,
                )
                if running is not None:
                    self._maintenance.dispatched[source_id] = running.id
                    return
                if dispatched is not None:
                    return
                job = next(
                    (
                        candidate
                        for candidate in live_jobs
                        if candidate.id in current_ids and candidate.status == JobStatus.QUEUED
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
        self._maintenance.dispatched[source_id] = job_id
        await self._queue.put((priority, next(self._enqueue_sequence), job_id))

    async def _close_source_maintenance(self, source_id: str) -> None:
        if source_id in self._maintenance.closing:
            return
        self._maintenance.closing.add(source_id)

        from sag_api.db.models import Job, Source

        current_task = asyncio.current_task()
        coordinator = self._maintenance.tasks.get(source_id)
        closing_in_coordinator = coordinator is current_task
        try:
            if coordinator is not None and not closing_in_coordinator and not coordinator.done():
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
                                    select(Job)
                                    .where(
                                        Job.source_id == source_id,
                                        Job.type == JobType.PROCESS_DOCUMENT,
                                        Job.status == JobStatus.QUEUED,
                                    )
                                    .join(
                                        Document,
                                        Job.document_id == Document.id,
                                    )
                                    .where(
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
                            if candidate_source is None or candidate_source.is_deleting:
                                continue
                            if get_blocked_reason(blocked.payload) != SOURCE_MAINTENANCE:
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
            while source_id in self._maintenance.ready and source is not None:
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

            self._maintenance.ready.discard(source_id)
            self._maintenance.closing.discard(source_id)
            if self.source_maintenance_requested(source_id):
                # A new control request can arrive while the old window closes.
                # The current coordinator continues its loop; a worker-owned
                # close starts a fresh coordinator after the old one is gone.
                if not closing_in_coordinator:
                    self._schedule_source_maintenance(source_id)
            else:
                self._maintenance.jobs.pop(source_id, None)
            for ready_id in ready_ids:
                await self.enqueue_durably(ready_id)
        finally:
            # Never leave a source permanently fenced if database or engine
            # cleanup is cancelled during shutdown.
            self._maintenance.closing.discard(source_id)

    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        active = self._maintenance.jobs.get(source_id)
        if active is not None:
            active.discard(job_id)
        if self._maintenance.dispatched.get(source_id) == job_id:
            self._maintenance.dispatched.pop(source_id, None)
        if active:
            if source_id in self._maintenance.ready:
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
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"sag-worker-{i}"))
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
        maintenance_tasks = list(self._maintenance.tasks.values())
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
        self._maintenance.tasks.clear()
        self._retry_tasks.clear()
        self._workers.clear()

        ready_source_ids = tuple(self._maintenance.ready)
        if ready_source_ids:
            from sag_api.db.models import Source

            sources = {}
            try:
                async with self._session_factory() as session:
                    sources = {source_id: await session.get(Source, source_id) for source_id in ready_source_ids}
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
        self._maintenance.jobs.clear()
        self._maintenance.ready.clear()
        self._maintenance.dispatched.clear()
        self._maintenance.closing.clear()
        self._started = False

    async def _recover(self) -> None:
        """Read receipts and gate unfinished document work until a manual action."""
        from sag_api.db.models import Job, Source
        from sag_api.services.document_service import qualify_historical_publications, reconcile_document_receipt

        manual_types = {
            JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT, JobType.DELETE_DOCUMENT, JobType.DELETE_SOURCE,
        }
        dispatch_ids = []
        for attempt in range(_RECOVERY_LOCK_RETRIES):
            try:
                async with self._session_factory() as session:
                    await qualify_historical_publications(session)
                    await session.commit()
                    rows = list(
                        (
                            await session.scalars(
                                select(Job).where(
                                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED])
                                )
                            )
                        ).all()
                    )
                    active_by_document = {}
                    for candidate in rows:
                        target = candidate.document_id or str((candidate.payload or {}).get("target_document_id") or "")
                        if target and candidate.type in manual_types:
                            active_by_document.setdefault((target, candidate.type), []).append(candidate)
                    for candidates in active_by_document.values():
                        keeper = max(
                            candidates,
                            key=lambda candidate: (
                                bool((candidate.payload or {}).get("process_request")),
                                candidate.started_at is not None,
                                candidate.created_at,
                                candidate.id,
                            ),
                        )
                        for duplicate in candidates:
                            if duplicate.id == keeper.id:
                                continue
                            duplicate.status = JobStatus.FAILED
                            duplicate.error = "任务已被较新的文档任务取代"
                            duplicate.finished_at = _now()
                            duplicate.payload = {**(duplicate.payload or {}), "superseded_by_job_id": keeper.id}
                    await session.commit()
                    for job in rows:
                        await session.refresh(job)
                        if job.status == JobStatus.FAILED:
                            continue
                        if job.type not in manual_types:
                            if job.status == JobStatus.RUNNING:
                                job.status = JobStatus.QUEUED
                                await session.commit()
                            continue
                        job_record_id = job.id
                        target = job.document_id or str((job.payload or {}).get("target_document_id") or "")
                        document = await session.get(Document, target) if target else None
                        source = await session.get(Source, job.source_id) if job.source_id else None
                        saved = (job.payload or {}).get("process_request")
                        if (
                            job.type == JobType.PROCESS_DOCUMENT
                            and saved
                            and source is not None
                            and document is not None
                            and document.status not in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}
                        ):
                            try:
                                if await reconcile_document_receipt(
                                    session, source, document, job, self._engine_manager,
                                ):
                                    await session.commit()
                                    continue
                                else:
                                    await session.refresh(job)
                                    await session.refresh(document)
                            except Exception as error:  # noqa: BLE001 - one unreadable receipt cannot start or erase work
                                log.warning(
                                    "启动读取文档结果失败 job=%s error_type=%s", job_record_id, type(error).__name__
                                )
                                await session.rollback()
                                await session.refresh(job)
                                if document is not None:
                                    await session.refresh(document)
                        payload = dict(job.payload or {})
                        payload.pop("resume_requested", None)
                        job.payload = set_scheduler(payload, blocked_reason=MANUAL_RECOVERY)
                        job.status = JobStatus.PAUSED
                        job.finished_at = None
                        if document is not None and document.status not in {
                            DocumentStatus.READY,
                            DocumentStatus.DELETING,
                            DocumentStatus.DELETE_FAILED,
                        }:
                            document.status = DocumentStatus.PAUSED
                            document.processing_stage = "waiting_manual"
                            document.processing_run_id = None
                        await session.commit()
                    # A lost cleanup Job must not cause an orphan to be deleted automatically.
                    deleting = list(
                        (
                            await session.scalars(
                                select(Document).join(Source, Document.source_id == Source.id).where(
                                    Document.status.in_([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
                                    Source.is_deleting.is_(False),
                                )
                            )
                        ).all()
                    )
                    for document in deleting:
                        cleanup = await session.scalar(
                            select(Job.id)
                            .where(
                                Job.document_id == document.id,
                                Job.type == JobType.DELETE_DOCUMENT,
                                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED]),
                            )
                            .limit(1)
                        )
                        if cleanup is None:
                            session.add(
                                Job(
                                    type=JobType.DELETE_DOCUMENT,
                                    source_id=document.source_id,
                                    document_id=document.id,
                                    status=JobStatus.PAUSED,
                                    payload=set_scheduler(
                                        {"target_document_id": document.id},
                                        priority=DELETE_PRIORITY,
                                        blocked_reason=MANUAL_RECOVERY,
                                    ),
                                )
                            )
                    await session.commit()
                    pending = list(
                        (
                            await session.scalars(
                                select(Job)
                                .where(
                                    Job.status == JobStatus.QUEUED,
                                )
                                .order_by(Job.created_at, Job.id)
                            )
                        ).all()
                    )
                    dispatch_ids = [job.id for job in pending if not get_blocked_reason(job.payload)]
                break
            except OperationalError as error:
                if "database is locked" not in str(error).lower() or attempt == _RECOVERY_LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(0.08 * (2**attempt))
        for job_id in dispatch_ids:
            await self.enqueue(job_id)

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
                str((job.payload or {}).get("user_id") or "") if job.type == JobType.INDEX_UNIVERSE else ""
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
            if job.type == JobType.REPROCESS_DOCUMENT:
                from sag_api.services.document_service import hold_document_for_restart

                if not await hold_document_for_restart(session, job, DocumentRestartRequiredError("legacy_checkpoint")):
                    job.status = JobStatus.PAUSED
                    job.payload = set_scheduler(
                        {**(job.payload or {}), "restart_required": "legacy_checkpoint"},
                        blocked_reason=MANUAL_RECOVERY,
                    )
                    await session.commit()
                return
            payload = dict(job.payload or {})
            if get_blocked_reason(payload) == MANUAL_RECOVERY:
                return
            if job.type == JobType.PROCESS_DOCUMENT and job.source_id:
                source = await session.get(Source, job.source_id, populate_existing=True)
                if source is None or source.is_deleting:
                    job.status = JobStatus.PAUSED
                    job.payload = set_scheduler(payload, blocked_reason=SOURCE_MAINTENANCE)
                    await session.commit()
                    return
            if job.type == JobType.PROCESS_DOCUMENT and job.document_id:
                document = await session.get(Document, job.document_id, populate_existing=True)
                if document is not None:
                    payload.setdefault("control_revision", document.control_revision)
                    if payload["control_revision"] != document.control_revision:
                        job.status = JobStatus.PAUSED
                        await session.commit()
                        return
            is_resume = bool(payload.pop("resume_requested", False))
            payload.pop("failure_retryable", None)
            previous_attempts = job.attempts
            claim = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(
                    payload=payload,
                    status=JobStatus.RUNNING,
                    started_at=_now(),
                    finished_at=None,
                    attempts=max(job.attempts, 1) if is_resume else job.attempts + 1,
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
                release_source_maintenance = bool(succeeded.rowcount and claimed_type in _MAINTENANCE_JOB_TYPES)
            except asyncio.CancelledError:
                raise
            except JobDeleted:
                if claimed_type in _MAINTENANCE_JOB_TYPES and claimed_source_id:
                    await self.finish_source_maintenance(claimed_source_id, job_id)
                return
            except JobYielded as yielded:
                await session.rollback()
                job = await session.get(Job, job_id)
                if job is not None and job.status == JobStatus.PAUSED and job.started_at == claimed_started_at:
                    await _converge_document_paused(session, job)
                    log.info("任务已被并发暂停，忽略让行信号 job=%s", job_id)
                elif job is not None:
                    busy = yielded.reason == SOURCE_BUSY
                    payload = set_scheduler(
                        job.payload,
                        blocked_reason=None if busy else yielded.reason,
                    )
                    if busy and is_resume:
                        payload["resume_requested"] = True
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
                            attempts=previous_attempts if busy else job.attempts,
                        )
                    )
                    if yielded_update.rowcount:
                        await _mark_document_queued(session, job)
                        if busy:
                            retry_delay_to_schedule = 0.25
                            retry_source_maintenance = claimed_type in _MAINTENANCE_JOB_TYPES
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
            except DocumentRestartRequiredError as error:
                from sag_api.services.document_service import hold_document_for_restart

                await session.rollback()
                job = await session.get(Job, job_id)
                if job is not None and job.status == JobStatus.RUNNING and job.started_at == claimed_started_at:
                    if await hold_document_for_restart(session, job, error):
                        log.info("文档等待确认重跑 job=%s reason=%s", job_id, error.reason)
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                job = await session.get(Job, job_id)
                if job is not None and job.status == JobStatus.PAUSED and job.started_at == claimed_started_at:
                    await _converge_document_paused(session, job)
                    log.info("任务已被并发暂停，忽略 handler 失败 job=%s", job_id)
                    await session.commit()
                    return
                msg = getattr(e, "message", None) or str(e)
                attempts = job.attempts if job is not None else settings.job_max_attempts
                delete_cleanup = bool(job is not None and job.type in _MAINTENANCE_JOB_TYPES)
                retry = job is not None and (_is_retryable(e) and attempts < settings.job_max_attempts)
                if job is not None and job.status == JobStatus.RUNNING:
                    if retry:
                        # This control CAS takes the document write lock before
                        # requeueing. A won deletion/retirement cannot be undone.
                        retry = await _mark_document_waiting_retry(session, job)
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
                            if delete_cleanup and job.document_id:
                                document = await session.get(Document, job.document_id)
                                if document is not None:
                                    document.status = DocumentStatus.DELETING
                                    document.error = None
                            if job.type == JobType.DELETE_SOURCE and claimed_source_id:
                                source = await session.get(Source, claimed_source_id)
                                if source is not None:
                                    source.status = SourceStatus.DELETING
                            retry_delay_to_schedule = delay
                            retry_source_maintenance = claimed_type in _MAINTENANCE_JOB_TYPES
                            release_source_maintenance = bool(delete_cleanup and claimed_source_id)
                            log.warning(
                                "任务可重试 job=%s（第 %d/%d 次），%.1fs 后重排：%s",
                                job_id,
                                attempts,
                                settings.job_max_attempts,
                                delay,
                                msg,
                            )
                    else:
                        failure_values = {"status": JobStatus.FAILED, "error": msg, "finished_at": _now()}
                        failure_transition = update(Job).where(
                            Job.id == job_id,
                            Job.status == JobStatus.RUNNING,
                            Job.started_at == claimed_started_at,
                        )
                        if job.type == JobType.PROCESS_DOCUMENT:
                            failure_values["payload"] = {**(job.payload or {}), "failure_retryable": _is_retryable(e)}
                            failure_transition = failure_transition.where(job_payload_matches(session, job.payload))
                        failed_update = await session.execute(failure_transition.values(**failure_values))
                        if failed_update.rowcount:
                            if job.type == JobType.DELETE_DOCUMENT and job.document_id:
                                document = await session.get(Document, job.document_id)
                                if document is not None:
                                    document.status = DocumentStatus.DELETE_FAILED
                                    document.error = msg
                                release_source_maintenance = bool(claimed_source_id)
                            if job.type == JobType.DELETE_SOURCE:
                                source = await session.get(Source, claimed_source_id) if claimed_source_id else None
                                if source is not None:
                                    source.status = SourceStatus.DELETE_FAILED
                                release_source_maintenance = bool(claimed_source_id)
                            log.warning(
                                "任务失败 job=%s（尝试 %d 次）：%s",
                                job_id,
                                attempts,
                                msg,
                            )
            if claimed_type in _MAINTENANCE_JOB_TYPES and claimed_source_id:
                # Cleanup may already have committed its terminal state with
                # the record deletion, including before a late lease-exit error.
                completed = await session.scalar(select(Job.id).where(
                    Job.id == job_id, Job.status == JobStatus.SUCCEEDED, Job.started_at == claimed_started_at,
                ))
                release_source_maintenance = release_source_maintenance or completed is not None
            await session.commit()
            if retry_delay_to_schedule is not None:
                self._schedule_retry(job_id, retry_delay_to_schedule)
            if retry_source_maintenance and claimed_source_id:
                if self._maintenance.dispatched.get(claimed_source_id) == job_id:
                    self._maintenance.dispatched.pop(claimed_source_id, None)
            if release_source_maintenance and claimed_source_id:
                await self.finish_source_maintenance(claimed_source_id, job_id)
