from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from sag_api.core.config import settings
from sag_api.runtime import KnowledgeRuntime
from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.contracts import (
    StorageBootstrapPhase,
    StorageBootstrapStatus,
    StorageChoice,
)
from sag_api.upgrades.coordinator import StorageBootstrapCoordinator
from sag_api.upgrades.state import BootstrapState
from sag_api.upgrades.types import StorageLayout, StorageVersion


class _LifecycleDependency:
    def __init__(self, factory: _RuntimeFactory, name: str) -> None:
        self.factory = factory
        self.name = name

    async def start(self) -> None:
        self.factory.calls.append(f"{self.name}.start")
        if self.factory.fail_on == f"{self.name}.start":
            self.factory.fail_on = None
            raise RuntimeError(f"{self.name} failed")

    async def stop(self) -> None:
        self.factory.calls.append(f"{self.name}.stop")
        self.factory.maybe_fail(f"{self.name}.stop")


class _JobQueue(_LifecycleDependency):
    async def start(self) -> None:
        self.factory.job_queue_start_count += 1
        await super().start()

    async def stop(self) -> None:
        self.factory.job_queue_stop_count += 1
        await super().stop()


class _EngineManager:
    def __init__(self, factory: _RuntimeFactory, active_path: Path) -> None:
        self.factory = factory
        self.active_path = active_path

    async def aclose_all(self) -> None:
        self.factory.calls.append("engine_manager.stop")
        self.factory.maybe_fail("engine_manager.stop")


class _RuntimeFactory:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.engine_manager_count = 0
        self.job_queue_start_count = 0
        self.job_queue_stop_count = 0
        self.active_paths: list[Path] = []

    def maybe_fail(self, operation: str) -> None:
        if self.fail_on == operation:
            self.fail_on = None
            raise RuntimeError(f"{operation} failed")

    async def seed_default_agent(self) -> None:
        self.calls.append("seed")

    async def recover_octx_state(self) -> dict[str, int]:
        self.calls.append("recover")
        return {}

    def install_litellm_policy(self):
        self.calls.append("policy.install")
        return object()

    def uninstall_litellm_policy(self, _policy) -> None:
        self.calls.append("policy.uninstall")

    def create_engine_manager(self, _settings, active_path: Path):
        self.engine_manager_count += 1
        self.active_paths.append(active_path)
        return _EngineManager(self, active_path)

    def create_llm_client(self, _settings):
        self.calls.append("llm.create")
        return object()

    def create_agent_runtime(self):
        return _LifecycleDependency(self, "agent_runtime")

    def create_job_queue(self, _session_factory, _engine_manager, *, concurrency: int):
        assert concurrency == settings.job_concurrency
        return _JobQueue(self, "job_queue")

    def start_warmup(self, _engine_manager):
        self.calls.append("warmup.start")
        return None

    async def stop_warmup(self, _task) -> None:
        self.calls.append("warmup.stop")
        self.maybe_fail("warmup.stop")

    async def start_mcp(self, _app):
        self.calls.append("mcp.start")
        return None

    async def stop_mcp(self, _handle) -> None:
        self.calls.append("mcp.stop")
        self.maybe_fail("mcp.stop")


@pytest.mark.asyncio
async def test_runtime_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    factory = _RuntimeFactory()
    active_path = tmp_path / "active-engine"
    runtime = KnowledgeRuntime(settings, object(), active_path=active_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)

    await asyncio.gather(runtime.start(app), runtime.start(app))

    assert runtime.ready is True
    assert factory.engine_manager_count == 1
    assert factory.job_queue_start_count == 1
    assert factory.active_paths == [active_path]
    assert app.state.knowledge_runtime is runtime

    await asyncio.gather(runtime.stop(), runtime.stop())

    assert runtime.ready is False
    assert app.state.knowledge_runtime is None
    assert app.state.engine_manager is None
    assert app.state.llm is None
    assert app.state.agent_runtime is None
    assert app.state.job_queue is None
    assert factory.job_queue_stop_count == 1
    assert factory.calls[-6:] == [
        "mcp.stop",
        "warmup.stop",
        "job_queue.stop",
        "agent_runtime.stop",
        "engine_manager.stop",
        "policy.uninstall",
    ]


@pytest.mark.asyncio
async def test_partial_start_failure_cleans_started_resources(tmp_path: Path) -> None:
    factory = _RuntimeFactory(fail_on="job_queue.start")
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)

    with pytest.raises(RuntimeError, match="job_queue failed"):
        await runtime.start(app)

    assert runtime.ready is False
    assert factory.calls[-3:] == [
        "agent_runtime.stop",
        "engine_manager.stop",
        "policy.uninstall",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        "mcp.stop",
        "warmup.stop",
        "job_queue.stop",
        "agent_runtime.stop",
        "engine_manager.stop",
    ),
)
async def test_teardown_failure_attempts_all_resources_and_retries_owner(
    tmp_path: Path,
    failure: str,
) -> None:
    factory = _RuntimeFactory(fail_on=failure)
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)
    await runtime.start(app)

    cleanup_start = len(factory.calls)
    with pytest.raises(BaseExceptionGroup) as exc_info:
        await runtime.stop()

    assert [str(error) for error in exc_info.value.exceptions] == [f"{failure} failed"]
    assert factory.calls[cleanup_start:] == [
        "mcp.stop",
        "warmup.stop",
        "job_queue.stop",
        "agent_runtime.stop",
        "engine_manager.stop",
        "policy.uninstall",
    ]
    retry_start = len(factory.calls)
    await runtime.stop()
    assert factory.calls[retry_start:] == [failure]


@pytest.mark.asyncio
async def test_coordinator_retry_drains_retained_runtime_before_replacement(
    tmp_path: Path,
) -> None:
    class FailingQueue(_JobQueue):
        async def start(self) -> None:
            if not getattr(self.factory, "startup_failed", False):
                self.factory.startup_failed = True
                self.factory.fail_on = "agent_runtime.stop"
                self.factory.calls.append("job_queue.start")
                raise RuntimeError("job_queue failed")
            await super().start()

    class Factory(_RuntimeFactory):
        def create_job_queue(self, _session_factory, _engine_manager, *, concurrency: int):
            assert concurrency == settings.job_concurrency
            return FailingQueue(self, "job_queue")

    factory = Factory()
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)
    coordinator = StorageBootstrapCoordinator(settings, object(), on_ready=lambda: runtime.start(app))
    coordinator.store.save(
        BootstrapState(
            phase=StorageBootstrapPhase.PROCESSING,
            source_version="current",
            target_version="0.8.2",
            choice=StorageChoice.MIGRATE,
            stage="verified",
        )
    )

    assert (await coordinator.inspect()).phase is StorageBootstrapPhase.PROCESSING
    await coordinator.wait()
    assert coordinator.store.load().phase is StorageBootstrapPhase.FAILED
    assert factory.engine_manager_count == 1
    assert coordinator.store.load().error == (
        "knowledge runtime startup failed (2 sub-exceptions)"
    )

    async def current_probe():
        return SimpleNamespace(version=StorageVersion.CURRENT)

    coordinator._probe = current_probe
    retry_start = len(factory.calls)
    await coordinator.choose(StorageChoice.MIGRATE, "owner")
    await coordinator.wait()

    assert factory.calls[retry_start] == "agent_runtime.stop"
    assert factory.calls[retry_start + 1] == "seed"
    assert factory.engine_manager_count == 2
    assert coordinator.store.load().phase is StorageBootstrapPhase.READY
    await runtime.stop()


    factory = _RuntimeFactory(fail_on="job_queue.start")
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    foreign = object()
    app = SimpleNamespace(
        state=SimpleNamespace(
            knowledge_runtime=runtime,
            engine_manager=foreign,
            llm=foreign,
            agent_runtime=foreign,
            job_queue=foreign,
        ),
        source_mcp=None,
    )

    with pytest.raises(RuntimeError, match="job_queue failed"):
        await runtime.start(app)

    assert vars(app.state) == {
        "knowledge_runtime": None,
        "engine_manager": foreign,
        "llm": foreign,
        "agent_runtime": foreign,
        "job_queue": foreign,
    }


@pytest.mark.asyncio
async def test_runtime_restart_publishes_new_dependencies(tmp_path: Path) -> None:
    factory = _RuntimeFactory()
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)

    await runtime.start(app)
    first_dependencies = (
        app.state.engine_manager,
        app.state.llm,
        app.state.agent_runtime,
        app.state.job_queue,
    )
    await runtime.stop()
    await runtime.start(app)

    assert runtime.ready is True
    assert app.state.knowledge_runtime is runtime
    assert all(
        current is not previous
        for current, previous in zip(
            (
                app.state.engine_manager,
                app.state.llm,
                app.state.agent_runtime,
                app.state.job_queue,
            ),
            first_dependencies,
            strict=True,
        )
    )

    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_applies_non_engine_settings_without_resetting_engines(
    tmp_path: Path,
) -> None:
    factory = _RuntimeFactory()
    active_path = tmp_path / "active-engine"
    runtime = KnowledgeRuntime(settings, object(), active_path=active_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)
    await runtime.start(app)
    engine_manager = app.state.engine_manager
    reset_count = factory.calls.count("engine_manager.stop")

    updated = settings.model_copy(update={"search_top_k": 17})
    await runtime.apply_settings(updated, reset_engines=False)

    assert runtime._settings.search_top_k == 17
    assert runtime._settings.data_dir == str(active_path)
    assert app.state.engine_manager is engine_manager
    assert factory.calls.count("engine_manager.stop") == reset_count
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_resets_cached_engines_after_engine_settings_change(
    tmp_path: Path,
) -> None:
    factory = _RuntimeFactory()
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)
    await runtime.start(app)
    reset_count = factory.calls.count("engine_manager.stop")

    updated = settings.model_copy(update={"llm_model": "replacement-model"})
    await runtime.apply_settings(updated, reset_engines=True)

    assert runtime._settings.llm_model == "replacement-model"
    assert factory.calls.count("engine_manager.stop") == reset_count + 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_stop_preserves_replaced_app_state(tmp_path: Path) -> None:
    factory = _RuntimeFactory()
    runtime = KnowledgeRuntime(settings, object(), active_path=tmp_path, factory=factory)
    app = SimpleNamespace(state=SimpleNamespace(), source_mcp=None)
    await runtime.start(app)
    foreign = object()
    app.state.engine_manager = foreign
    app.state.job_queue = foreign

    await runtime.stop()

    assert app.state.knowledge_runtime is None
    assert app.state.engine_manager is foreign
    assert app.state.job_queue is foreign


@pytest.mark.asyncio
async def test_ready_waits_for_bootstrap_callback_runtime_install(monkeypatch) -> None:
    from sag_api import main as main_module

    runtime = SimpleNamespace(ready=False, starts=0, stops=0)

    async def start(_app) -> None:
        runtime.starts += 1
        runtime.ready = True

    async def stop() -> None:
        runtime.stops += 1
        runtime.ready = False

    runtime.start = start
    runtime.stop = stop

    class Coordinator:
        instance = None

        def __init__(self, _settings, _sessions, *, on_ready):
            self.on_ready = on_ready
            self.phase = StorageBootstrapPhase.CHOICE_REQUIRED
            Coordinator.instance = self

        async def inspect(self):
            return StorageBootstrapStatus(
                self.phase,
                "legacy_0_7",
                "0.8.2",
                runtime_ready=self.phase is StorageBootstrapPhase.READY,
            )

        async def complete_choice(self) -> None:
            await self.on_ready()
            self.phase = StorageBootstrapPhase.READY

        async def wait(self) -> None:
            return None

        def runtime_ready(self) -> bool:
            return self.phase is StorageBootstrapPhase.READY

        def public_status(self, *, authenticated: bool = False):
            del authenticated
            return {"phase": self.phase.value, "runtime_ready": self.runtime_ready()}

    monkeypatch.setattr(
        "sag_api.upgrades.integration.StorageBootstrapCoordinator", Coordinator
    )
    monkeypatch.setattr(
        "sag_api.upgrades.integration.KnowledgeRuntime", lambda *_args, **_kwargs: runtime
    )
    monkeypatch.setattr(main_module, "init_db", _noop)
    monkeypatch.setattr(main_module, "dispose_db", _noop)
    monkeypatch.setattr(
        "sag_api.services.settings_service.apply_startup_overrides", _noop
    )
    app = main_module.create_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            waiting = await client.get("/api/v1/system/ready")
            assert waiting.status_code == 503
            assert waiting.json() == {
                "status": "unavailable",
                "db": True,
                "phase": "choice_required",
            }

            await Coordinator.instance.complete_choice()

            ready = await client.get("/api/v1/system/ready")
            assert ready.status_code == 200
            assert ready.json() == {"status": "ready", "db": True}
            assert runtime.starts == 1

    assert runtime.stops == 1


@pytest.mark.asyncio
async def test_lifespan_disposes_database_after_runtime_stop_failure(monkeypatch) -> None:
    from sag_api import main as main_module

    disposed = False

    class Runtime:
        ready = False

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self, _app) -> None:
            self.ready = True

        async def stop(self) -> None:
            self.ready = False
            raise RuntimeError("runtime stop failed")

    async def dispose() -> None:
        nonlocal disposed
        disposed = True

    monkeypatch.setattr("sag_api.upgrades.integration.KnowledgeRuntime", Runtime)
    monkeypatch.setattr(main_module, "init_db", _noop)
    monkeypatch.setattr(main_module, "dispose_db", dispose)
    monkeypatch.setattr(
        "sag_api.services.settings_service.apply_startup_overrides", _noop
    )
    app = main_module.create_app()

    with pytest.raises(BaseExceptionGroup, match="application shutdown failed") as exc_info:
        async with app.router.lifespan_context(app):
            pass

    assert disposed is True
    assert [str(error) for error in exc_info.value.exceptions] == [
        "runtime stop failed"
    ]


@pytest.mark.asyncio
async def test_real_ready_coordinator_installs_resolved_runtime_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sag_api import main as main_module

    configured_path = tmp_path / "configured-engine"
    active_path = tmp_path / "active-engine"
    configured_path.mkdir()
    active_path.mkdir()
    test_settings = settings.model_copy(
        update={
            "data_dir": str(configured_path),
            "upload_dir": str(tmp_path / "uploads"),
        }
    )
    layout = StorageLayout.from_settings(test_settings)
    ActiveEngineStore(layout.upgrades / "active-engine.json").activate(
        configured_path,
        active_path,
    )
    runtime = SimpleNamespace(ready=False, starts=0, stops=0, active_path=None)

    class Runtime:
        def __init__(self, _settings, _sessions, *, active_path: Path) -> None:
            runtime.active_path = active_path

        async def start(self, _app) -> None:
            runtime.starts += 1
            runtime.ready = True

        async def stop(self) -> None:
            runtime.stops += 1
            runtime.ready = False

        @property
        def ready(self) -> bool:
            return runtime.ready

    monkeypatch.setattr(main_module, "settings", test_settings)
    monkeypatch.setattr(
        "sag_api.upgrades.integration.StorageBootstrapCoordinator",
        StorageBootstrapCoordinator,
    )
    monkeypatch.setattr("sag_api.upgrades.integration.KnowledgeRuntime", Runtime)
    monkeypatch.setattr(main_module, "init_db", _noop)
    monkeypatch.setattr(main_module, "dispose_db", _noop)
    monkeypatch.setattr("sag_api.api.v1.system.SessionLocal", _ReadySessionFactory())
    monkeypatch.setattr(
        "sag_api.services.settings_service.apply_startup_overrides",
        _noop,
    )
    app = main_module.create_app()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/system/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "db": True}
        assert runtime.active_path == active_path.resolve()
        assert runtime.starts == 1
        assert test_settings.data_dir == str(configured_path)
        assert test_settings.effective_data_dir == str(active_path.resolve())

    assert runtime.stops == 1


class _ReadySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def execute(self, *_args) -> None:
        return None


class _ReadySessionFactory:
    def __call__(self):
        return _ReadySession()


async def _noop(*_args, **_kwargs) -> None:
    return None
