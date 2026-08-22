"""Knowledge runtime lifecycle, installed only after storage bootstrap is ready."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from sag_agent import AgentRuntime
from sag_api.core.litellm_policy import install_litellm_policy, uninstall_litellm_policy
from sag_api.core.logging import get_logger
from sag_api.generation import LLMClient
from sag_api.jobs import InProcessAsyncQueue
from sag_api.sag import EngineManager

log = get_logger("runtime")


class _RuntimeFactory:
    def __init__(self, settings: Any, session_factory: Any) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def seed_default_agent(self) -> None:
        from sag_api.services.agent_domain import get_default_agent

        async with self.session_factory() as session:
            await get_default_agent(session)

    async def recover_octx_state(self) -> dict[str, int]:
        from sag_api.services.octx_recovery_service import recover_octx_state

        async with self.session_factory() as session:
            return await recover_octx_state(session)

    def install_litellm_policy(self) -> Any:
        return install_litellm_policy(self.settings)

    def uninstall_litellm_policy(self, policy: Any) -> None:
        uninstall_litellm_policy(policy)

    def create_engine_manager(self, settings: Any, active_path: Path) -> EngineManager:
        del active_path
        return EngineManager(settings)

    def create_llm_client(self, settings: Any) -> LLMClient:
        return LLMClient(settings)

    def create_agent_runtime(self) -> AgentRuntime:
        return AgentRuntime()

    def create_job_queue(
        self,
        session_factory: Any,
        engine_manager: EngineManager,
        *,
        concurrency: int,
    ) -> InProcessAsyncQueue:
        return InProcessAsyncQueue(session_factory, engine_manager, concurrency=concurrency)

    def start_warmup(self, engine_manager: EngineManager) -> asyncio.Task:
        return asyncio.create_task(_warmup_engines(engine_manager, self.settings, self.session_factory))

    async def stop_warmup(self, task: asyncio.Task | None) -> None:
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def start_mcp(self, app: Any) -> AsyncExitStack | None:
        source_mcp = getattr(app.state, "source_mcp", None)
        if source_mcp is None:
            return None
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(source_mcp.session_manager.run())
        except Exception as error:  # noqa: BLE001
            await stack.aclose()
            log.warning("MCP 会话管理器启动失败（/mcp 不可用）：%s", error)
            return None
        log.info("MCP 端点已就绪 · /mcp/（全库）· 可选 ?source_id=<信源 id>")
        return stack

    async def stop_mcp(self, stack: AsyncExitStack | None) -> None:
        if stack is not None:
            await stack.aclose()


class KnowledgeRuntime:
    """Own the knowledge-serving resources behind bootstrap readiness."""

    def __init__(
        self,
        settings: Any,
        session_factory: Any,
        *,
        active_path: Path,
        factory: Any | None = None,
    ) -> None:
        self._settings = settings.model_copy(update={"data_dir": str(active_path)})
        self._session_factory = session_factory
        self._active_path = active_path
        self._factory = factory or _RuntimeFactory(self._settings, session_factory)
        self._lock = asyncio.Lock()
        self._ready = False
        self._app: Any | None = None
        self._policy: Any | None = None
        self._engine_manager: Any | None = None
        self._llm: Any | None = None
        self._agent_runtime: Any | None = None
        self._job_queue: Any | None = None
        self._warmup_task: Any | None = None
        self._warmup_started = False
        self._mcp_handle: Any | None = None
        self._mcp_started = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self, app: Any) -> None:
        async with self._lock:
            if self._ready:
                return
            if self._has_started_resources():
                await self._cleanup()
            self._app = app
            try:
                await self._factory.seed_default_agent()
                recovery = await self._factory.recover_octx_state()
                if any(recovery.values()):
                    log.info("OCTX 启动恢复完成 %s", recovery)

                self._policy = self._factory.install_litellm_policy()
                self._engine_manager = self._factory.create_engine_manager(
                    self._settings, self._active_path
                )
                self._llm = self._factory.create_llm_client(self._settings)
                self._agent_runtime = self._factory.create_agent_runtime()
                await self._agent_runtime.start()
                self._job_queue = self._factory.create_job_queue(
                    self._session_factory,
                    self._engine_manager,
                    concurrency=self._settings.job_concurrency,
                )
                await self._job_queue.start()
                self._warmup_task = self._factory.start_warmup(self._engine_manager)
                self._warmup_started = True
                self._mcp_handle = await self._factory.start_mcp(app)
                self._mcp_started = True

                app.state.knowledge_runtime = self
                app.state.engine_manager = self._engine_manager
                app.state.llm = self._llm
                app.state.agent_runtime = self._agent_runtime
                app.state.job_queue = self._job_queue
                self._app = app
                self._ready = True
                log.info(
                    "sag-api 知识运行时已启动 · env=%s · llm_configured=%s · vector=%s",
                    self._settings.environment,
                    self._settings.llm_configured,
                    self._settings.sag_vector_provider,
                )
            except BaseException as startup_error:
                try:
                    await self._cleanup()
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "knowledge runtime startup failed",
                        [startup_error, cleanup_error],
                    ) from startup_error
                raise

    async def stop(self) -> None:
        async with self._lock:
            if not self._has_started_resources():
                self._ready = False
                return
            self._ready = False
            await self._cleanup()

    def _has_started_resources(self) -> bool:
        return any(
            (
                self._policy is not None,
                self._engine_manager is not None,
                self._agent_runtime is not None,
                self._job_queue is not None,
                self._warmup_started,
                self._mcp_started,
            )
        )

    async def _cleanup(self) -> None:
        self._withdraw_app_state()
        failures: list[BaseException] = []

        async def teardown(operation, release) -> None:
            try:
                await operation()
            except BaseException as error:
                failures.append(error)
            else:
                release()

        if self._mcp_started:
            await teardown(
                lambda: self._factory.stop_mcp(self._mcp_handle),
                self._release_mcp,
            )
        if self._warmup_started:
            await teardown(
                lambda: self._factory.stop_warmup(self._warmup_task),
                self._release_warmup,
            )
        if self._job_queue is not None:
            await teardown(self._job_queue.stop, lambda: setattr(self, "_job_queue", None))
        if self._agent_runtime is not None:
            await teardown(
                self._agent_runtime.stop,
                lambda: setattr(self, "_agent_runtime", None),
            )
        if self._engine_manager is not None:
            await teardown(
                self._engine_manager.aclose_all,
                lambda: setattr(self, "_engine_manager", None),
            )
        if self._policy is not None:
            try:
                self._factory.uninstall_litellm_policy(self._policy)
            except BaseException as error:
                failures.append(error)
            else:
                self._policy = None

        self._llm = None
        self._ready = False
        if failures:
            raise BaseExceptionGroup("knowledge runtime teardown failed", failures)

    def _withdraw_app_state(self) -> None:
        app = self._app
        self._app = None
        if app is None:
            return
        owned = {
            "knowledge_runtime": self,
            "engine_manager": self._engine_manager,
            "llm": self._llm,
            "agent_runtime": self._agent_runtime,
            "job_queue": self._job_queue,
        }
        for name, resource in owned.items():
            if getattr(app.state, name, None) is resource:
                setattr(app.state, name, None)

    def _release_mcp(self) -> None:
        self._mcp_handle = None
        self._mcp_started = False

    def _release_warmup(self) -> None:
        self._warmup_task = None
        self._warmup_started = False


async def _warmup_engines(engine_manager: EngineManager, settings: Any, session_factory: Any) -> None:
    """Warm recently updated source engines without blocking runtime startup."""
    if settings.engine_warmup_count <= 0:
        return
    try:
        from sqlalchemy import select

        from sag_api.db.models import Source

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Source).order_by(Source.updated_at.desc()).limit(settings.engine_warmup_count)
                    )
                )
                .scalars()
                .all()
            )
        for source in rows:
            try:
                await engine_manager.provision(source.sag_source_config_id, source)
            except Exception as error:  # noqa: BLE001
                log.warning("预热引擎失败 source=%s: %s", source.id, error)
        if rows:
            log.info("已预热 %d 个信源引擎", len(rows))
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        log.warning("引擎预热任务异常：%s", error)
