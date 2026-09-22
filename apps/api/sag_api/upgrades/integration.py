"""Application seam for explicit knowledge rebuilds and active-engine startup.

Active-engine pointer resolution remains necessary for workspaces created by
past migrations or rebuilds. Keep this integration when removing old migration
adapters; it installs the selected runtime and enforces maintenance gating.
"""

from __future__ import annotations

import asyncio

from sag_api.runtime import KnowledgeRuntime
from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.coordinator import StorageBootstrapCoordinator
from sag_api.upgrades.middleware import StorageMaintenanceMiddleware
from sag_api.upgrades.types import StorageLayout


class StorageBootstrapController:
    """Small holder exposing the bootstrap lifecycle to the app lifespan.

    Owns the StorageLayout/ActiveEngineStore construction, the runtime install
    factory (resolving the active engine path and constructing
    KnowledgeRuntime), the StorageBootstrapCoordinator with its `on_ready`
    callback, and the `app.state.storage_bootstrap` /
    `app.state.knowledge_runtime` publication. `bind_storage_bootstrap`
    returns this holder so the lifespan can call `inspect()` / `wait()` and
    trigger the ready install.
    """

    def __init__(self, app, settings, session_factory) -> None:
        self.app = app
        self.settings = settings
        self.session_factory = session_factory
        self.layout = StorageLayout.from_settings(settings)
        self.active_store = ActiveEngineStore(self.layout.upgrades / "active-engine.json")
        self.runtime: KnowledgeRuntime | None = None
        self.runtime_install_lock = asyncio.Lock()
        self.coordinator = StorageBootstrapCoordinator(
            settings,
            session_factory,
            on_ready=self.install_runtime,
        )
        app.state.storage_bootstrap = self.coordinator
        app.state.knowledge_runtime = None

    async def inspect(self):
        return await self.coordinator.inspect()

    async def install_runtime(self) -> None:
        """Resolve the active engine and (re)start the knowledge runtime once."""
        async with self.runtime_install_lock:
            active_path = self.active_store.resolve(self.layout.engine)
            self.settings.activate_data_dir(active_path)
            if self.runtime is None:
                self.runtime = KnowledgeRuntime(
                    self.settings,
                    self.session_factory,
                    active_path=active_path,
                )
                self.app.state.knowledge_runtime = self.runtime
            await self.runtime.start(self.app)

    async def wait(self) -> None:
        """Drain a running upgrade task, if any (lifespan teardown)."""
        await self.coordinator.wait()

    async def stop_runtime(self) -> None:
        """Stop the installed runtime, if any (lifespan teardown)."""
        if self.runtime is not None:
            await self.runtime.stop()


def bind_storage_bootstrap(app, settings, session_factory) -> StorageBootstrapController:
    """Create the bootstrap coordinator, publish app state, return its holder."""
    return StorageBootstrapController(app, settings, session_factory)


def install_storage_bootstrap_middleware(app) -> None:
    """Register the maintenance-mode middleware (create_app wiring)."""
    app.add_middleware(StorageMaintenanceMiddleware)
