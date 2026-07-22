"""进程内 asyncio 任务队列 —— 随 API 进程起停。

- N 个 worker 协程从队列取 job_id，加载 Job，维护状态机并分发处理器。
- 启动时「恢复」上次残留的 QUEUED/RUNNING 任务（RUNNING 重置为 QUEUED 重跑）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from sag_api.core.config import settings
from sag_api.core.errors import ServiceUnavailableError, UpstreamError
from sag_api.core.logging import get_logger
from sag_api.enums import JobStatus, JobType
from sag_api.jobs.control import JobPaused
from sag_api.jobs.queue import JobQueue
from sag_api.jobs.tasks import TASK_HANDLERS
from sag_api.sag import EngineManager

log = get_logger("jobs")

# 退避基数（秒）：第 n 次重试等待 base**n。测试可 monkeypatch 缩短。
_BACKOFF_BASE_SECONDS = 2.0
_RECOVERY_LOCK_RETRIES = 4


def _now() -> datetime:
    return datetime.now(UTC)


def _is_retryable(exc: Exception) -> bool:
    """瞬时故障（限流/超时/上游暂不可用）可重试；输入/配置类错误不重试。"""
    return isinstance(exc, (ServiceUnavailableError, UpstreamError))


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
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._retry_tasks: set[asyncio.Task] = set()
        self._universe_user_locks: dict[str, asyncio.Lock] = {}
        self._started = False

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def _schedule_retry(self, job_id: str, delay: float) -> None:
        """退避后重新入队（不阻塞 worker）。"""

        async def _later() -> None:
            try:
                await asyncio.sleep(delay)
                await self._queue.put(job_id)
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
        retry_tasks = list(self._retry_tasks)
        for t in retry_tasks:
            t.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._universe_user_locks.clear()
        self._started = False

    async def _recover(self) -> None:
        from sag_api.db.models import Job

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
                    for job in rows:
                        if job.status == JobStatus.RUNNING:
                            job.status = JobStatus.QUEUED
                    await session.commit()
                break
            except OperationalError as error:
                locked = "database is locked" in str(error).lower()
                if not locked or attempt == _RECOVERY_LOCK_RETRIES - 1:
                    raise
                await asyncio.sleep(0.08 * (2**attempt))
        for job in rows:
            await self._queue.put(job.id)
        if rows:
            log.info("恢复 %d 个未完成任务", len(rows))

    async def _worker_loop(self, idx: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
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

            handler = TASK_HANDLERS.get(job.type)
            if handler is None:
                job.status = JobStatus.FAILED
                job.error = f"未知任务类型：{job.type}"
                job.finished_at = _now()
                await session.commit()
                return

            try:
                await handler(session, job, engine_manager=self._engine_manager, job_queue=self)
                job.status = JobStatus.SUCCEEDED
                job.progress = 1.0
                job.finished_at = _now()
                job.error = None
            except JobPaused:
                await session.rollback()
                job = await session.get(Job, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload.pop("pause_requested", None)
                    job.payload = payload
                    job.status = JobStatus.PAUSED
                    job.finished_at = None
                    job.error = None
                    log.info("任务已暂停 job=%s progress=%.0f%%", job_id, job.progress * 100)
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                job = await session.get(Job, job_id)
                msg = getattr(e, "message", None) or str(e)
                attempts = job.attempts if job is not None else settings.job_max_attempts
                retry = job is not None and _is_retryable(e) and attempts < settings.job_max_attempts
                if job is not None:
                    if retry:
                        # 退避重排：状态回 QUEUED，延迟 base**attempts 秒后重新入队
                        job.status = JobStatus.QUEUED
                        job.progress = 0.0
                        job.error = f"第 {attempts} 次失败，将重试：{msg}"
                        delay = _BACKOFF_BASE_SECONDS**attempts
                        self._schedule_retry(job_id, delay)
                        log.warning(
                            "任务可重试 job=%s（第 %d/%d 次），%.1fs 后重排：%s",
                            job_id, attempts, settings.job_max_attempts, delay, msg,
                        )
                    else:
                        job.status = JobStatus.FAILED
                        job.error = msg
                        job.finished_at = _now()
                        log.warning("任务失败 job=%s（尝试 %d 次）：%s", job_id, attempts, msg)
            await session.commit()
