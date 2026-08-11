"""任务队列抽象。

MVP 用进程内 asyncio 队列（`InProcessAsyncQueue`）；接口保持精简，
未来可实现 Celery / RQ / Arq 等分布式后端而不影响调用方。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job_id: str) -> None:
        """把一个已持久化的 Job 投入队列等待执行。"""

    async def enqueue_durably(self, job_id: str) -> None:
        """派发已提交任务；支持后台重排的队列可覆盖此默认实现。"""
        await self.enqueue(job_id)

    @abstractmethod
    def begin_source_maintenance(self, source_id: str, job_id: str) -> None:
        """登记某个信源存在需要优先处理的维护任务。"""

    @abstractmethod
    def source_maintenance_requested(self, source_id: str) -> bool:
        """当前信源是否需要文档处理任务在安全检查点让行。"""

    @abstractmethod
    async def finish_source_maintenance(self, source_id: str, job_id: str) -> None:
        """结束维护任务；最后一个维护任务结束后唤醒临时让行的任务。"""

    async def start(self) -> None:  # noqa: B027 - 可选生命周期钩子
        """启动后台 worker（如有）。"""

    async def stop(self) -> None:  # noqa: B027
        """优雅停止 worker。"""
