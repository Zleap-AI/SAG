from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.contracts import (
    StorageBootstrapPhase,
    StorageBootstrapStatus,
    StorageChoice,
    StorageUpgradeContext,
)
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.fresh_workspace import FreshKnowledgeWorkspaceAdapter
from sag_api.upgrades.journal import UpgradeLock
from sag_api.upgrades.registry import select_adapter
from sag_api.upgrades.state import BootstrapState, BootstrapStateStore
from sag_api.upgrades.types import StorageLayout, StorageUpgradeError, StorageVersion


class StorageBootstrapCoordinator:
    """Coordinate explicit storage selection without exposing adapter internals."""

    def __init__(
        self,
        settings,
        session_factory,
        *,
        on_ready: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.on_ready = on_ready
        self.layout = StorageLayout.from_settings(settings)
        self.store = BootstrapStateStore(self.layout.upgrades / "bootstrap.json")
        self.pointer = ActiveEngineStore(self.layout.upgrades / "active-engine.json")
        self.started_tasks = 0
        self._task: asyncio.Task | None = None
        self._status: StorageBootstrapStatus | None = None

    async def inspect(self) -> StorageBootstrapStatus:
        try:
            active = self.pointer.resolve(self.layout.engine)
            probe = detect_storage(self._layout_for(active), self.settings)
            state = self.store.load()
        except StorageUpgradeError as error:
            return self._publish_probe_failure(error)

        if state is not None and state.choice is not None:
            state.preserved_path = state.preserved_path or str(active)
            if state.phase is StorageBootstrapPhase.READY:
                if probe.version is StorageVersion.CURRENT:
                    self._status = self._status_from_state(state)
                    return self._status
                state.phase = StorageBootstrapPhase.FAILED
                state.stage = "verify"
                state.error = probe.reason
                self.store.save(state)
                self._status = self._status_from_state(state)
                return self._status

            if state.phase is StorageBootstrapPhase.FAILED:
                state.phase = StorageBootstrapPhase.PROCESSING
                state.stage = "verified" if probe.version is StorageVersion.CURRENT else "queued"
                state.error = None
                self.store.save(state)
                self._schedule(state)
                return self._status_from_state(state)

            if state.phase is StorageBootstrapPhase.PROCESSING:
                if probe.version is StorageVersion.CURRENT:
                    state.stage = "verified"
                    self.store.save(state)
                self._schedule(state)
                return self._status_from_state(state)

        adapter = select_adapter(probe, target_version="0.8.2")
        if adapter is not None:
            status = StorageBootstrapStatus(
                StorageBootstrapPhase.CHOICE_REQUIRED,
                probe.version.value,
                "0.8.2",
                (StorageChoice.MIGRATE, StorageChoice.FRESH),
                preserved_path=active,
            )
        else:
            ready = probe.version in (
                StorageVersion.EMPTY,
                StorageVersion.CURRENT,
                StorageVersion.UNSUPPORTED,
            )
            status = StorageBootstrapStatus(
                StorageBootstrapPhase.READY if ready else StorageBootstrapPhase.FAILED,
                probe.version.value,
                "0.8.2",
                error=None if ready else probe.reason,
                recoverable=not ready,
                runtime_ready=ready,
                preserved_path=active,
            )
        self._status = status
        self.store.save(self._state_from_status(status))
        return status

    async def choose(self, choice: StorageChoice, actor_user_id: str) -> StorageBootstrapStatus:
        with UpgradeLock(self.layout.upgrades / "bootstrap.lock", timeout=0):
            state = self.store.load()
            if state is not None and state.choice is not None:
                if state.choice is not choice:
                    raise StorageUpgradeError(
                        "a different storage choice is already in progress",
                        stage="choice",
                        recoverable=True,
                        diagnostic_path=self.store.path,
                    )
                if state.phase is StorageBootstrapPhase.FAILED:
                    state.phase = StorageBootstrapPhase.PROCESSING
                    state.error = None
                    state.stage = "queued"
                    self.store.save(state)
                    self._schedule(state)
                elif state.phase is StorageBootstrapPhase.PROCESSING:
                    self._schedule(state)
                return self._status_from_state(state)

            status = await self.inspect()
            if status.phase is not StorageBootstrapPhase.CHOICE_REQUIRED:
                return status
            state = BootstrapState(
                phase=StorageBootstrapPhase.PROCESSING,
                source_version=status.detected_version,
                target_version=status.target_version,
                choice=choice,
                actor_user_id=actor_user_id,
                adapter_id=(
                    "fresh-knowledge-workspace"
                    if choice is StorageChoice.FRESH
                    else "zleap-sag-0.7.1-to-0.8.2"
                ),
                stage="queued",
                preserved_path=str(status.preserved_path) if status.preserved_path else None,
                diagnostic_path=str(self.store.path),
            )
            self.store.save(state)
            self._schedule(state)
            return self._status_from_state(state)

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    def _schedule(self, state: BootstrapState) -> None:
        if self._task is None or self._task.done():
            self.started_tasks += 1
            self._task = asyncio.create_task(self._run(state))
        self._status = self._status_from_state(state)

    async def _run(self, state: BootstrapState) -> None:
        try:
            if state.stage != "verified":
                state.stage = "processing"
                self.store.save(state)
                context = StorageUpgradeContext(self.settings, self.session_factory)
                if state.choice is StorageChoice.FRESH:
                    report = await FreshKnowledgeWorkspaceAdapter().create(context)
                else:
                    probe = await self._probe()
                    adapter = select_adapter(probe, target_version="0.8.2")
                    if adapter is None:
                        if probe.version is not StorageVersion.CURRENT:
                            raise StorageUpgradeError(
                                "registered storage adapter is missing",
                                stage="select",
                                recoverable=True,
                            )
                        report = None
                    else:
                        report = await adapter.migrate(context)
                state.stage = "verified"
                if report is not None:
                    state.report = {
                        key: str(value) if isinstance(value, Path) else value
                        for key, value in asdict(report).items()
                    }
                self.store.save(state)

            if self.on_ready is None:
                self._status = self._status_from_state(state)
                return
            callback_result = self.on_ready()
            if asyncio.iscoroutine(callback_result):
                await callback_result
            state.phase = StorageBootstrapPhase.READY
            state.stage = "ready"
            state.error = None
            self.store.save(state)
            self._status = self._status_from_state(state)
        except Exception as error:  # noqa: BLE001
            state.phase = StorageBootstrapPhase.FAILED
            state.stage = getattr(error, "stage", state.stage or "unknown")
            state.error = str(error)
            self.store.save(state)
            self._status = self._status_from_state(state)

    async def _probe(self):
        active = self.pointer.resolve(self.layout.engine)
        return detect_storage(self._layout_for(active), self.settings)

    def _publish_probe_failure(self, error: StorageUpgradeError) -> StorageBootstrapStatus:
        status = StorageBootstrapStatus(
            StorageBootstrapPhase.FAILED,
            None,
            "0.8.2",
            stage=error.stage,
            error=str(error),
            recoverable=error.recoverable,
            runtime_ready=False,
        )
        self._status = status
        return status

    def public_status(self, *, authenticated: bool = False) -> dict[str, Any]:
        status = self._status or StorageBootstrapStatus(
            StorageBootstrapPhase.READY, None, "0.8.2", runtime_ready=True
        )
        result: dict[str, Any] = {
            "phase": status.phase.value,
            "detected_version": status.detected_version,
            "target_version": status.target_version,
            "choices": (
                [choice.value for choice in status.choices]
                if status.phase is StorageBootstrapPhase.CHOICE_REQUIRED
                else []
            ),
            "stage": status.stage,
            "error": status.error,
            "recoverable": status.recoverable,
            "runtime_ready": status.runtime_ready,
        }
        if authenticated:
            result["accepted_choice"] = (
                status.choices[0].value
                if status.phase in (StorageBootstrapPhase.PROCESSING, StorageBootstrapPhase.FAILED)
                and len(status.choices) == 1
                else None
            )
            result["preserved_path"] = str(status.preserved_path) if status.preserved_path else None
            result["diagnostic_path"] = str(self.store.path)
        return result

    def runtime_ready(self) -> bool:
        return bool(self._status and self._status.runtime_ready)

    def _layout_for(self, engine: Path) -> StorageLayout:
        return StorageLayout(
            self.layout.root,
            engine,
            self.layout.sag_db,
            self.layout.upgrades,
            self.layout.backups,
            self.layout.staging,
        )

    @staticmethod
    def _state_from_status(status: StorageBootstrapStatus) -> BootstrapState:
        return BootstrapState(
            phase=status.phase,
            source_version=status.detected_version,
            target_version=status.target_version,
            stage=status.stage,
            error=status.error,
            preserved_path=str(status.preserved_path) if status.preserved_path else None,
        )

    @staticmethod
    def _status_from_state(state: BootstrapState) -> StorageBootstrapStatus:
        return StorageBootstrapStatus(
            phase=state.phase,
            detected_version=state.source_version,
            target_version=state.target_version,
            choices=(state.choice,) if state.choice else (),
            stage=state.stage,
            error=state.error,
            recoverable=state.phase is StorageBootstrapPhase.FAILED,
            runtime_ready=state.phase is StorageBootstrapPhase.READY,
            preserved_path=Path(state.preserved_path) if state.preserved_path else None,
        )
