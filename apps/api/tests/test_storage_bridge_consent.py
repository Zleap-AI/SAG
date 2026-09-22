from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_storage_bootstrap_api import _fingerprint, _fixture
from zleap.sag import DataEngine

from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.contracts import StorageBootstrapPhase, StorageChoice
from sag_api.upgrades.coordinator import StorageBootstrapCoordinator
from sag_api.upgrades.state import BootstrapState


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["prompt", "windows_fresh"])
async def test_bridge_legacy_startup_never_implicitly_rebuilds(tmp_path: Path, policy: str) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    settings.storage_bootstrap_policy = policy
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    before = _fingerprint(engine)
    try:
        status = await coordinator.inspect()
        assert status.phase is StorageBootstrapPhase.CHOICE_REQUIRED
        assert StorageChoice.FRESH in status.choices
        assert coordinator.started_tasks == 0
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [StorageBootstrapPhase.PROCESSING, StorageBootstrapPhase.FAILED])
@pytest.mark.parametrize("inspect_first", [False, True])
@pytest.mark.parametrize("current_pointer", [False, True])
async def test_bridge_old_fresh_history_needs_new_confirmation(
    tmp_path: Path,
    phase: StorageBootstrapPhase,
    inspect_first: bool,
    current_pointer: bool,
) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    settings.storage_bootstrap_policy = "windows_fresh"
    coordinator = StorageBootstrapCoordinator(settings, sessions, on_ready=lambda: None)
    if current_pointer:
        target = tmp_path / "existing-current"
        runtime = DataEngine(build_engine_config(settings, overrides={"data_dir": str(target)}), health_check=False)
        try:
            await runtime.start()
        finally:
            await runtime.aclose()
        coordinator.pointer.activate(engine, target)
    coordinator.store.save(
        BootstrapState(
            phase=phase,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=StorageChoice.FRESH,
            actor_user_id="desktop-windows",
            stage="queued",
        )
    )
    payload = json.loads(coordinator.store.path.read_text())
    payload.pop("rebuild_confirmed", None)
    coordinator.store.path.write_text(json.dumps(payload))
    before = _fingerprint(engine)
    try:
        if inspect_first:
            for _ in range(2):
                status = await coordinator.inspect()
                assert status.phase is StorageBootstrapPhase.CHOICE_REQUIRED
                assert StorageChoice.FRESH in status.choices
                assert coordinator.started_tasks == 0
            assert _fingerprint(engine) == before
        status = await coordinator.choose(StorageChoice.FRESH, "authenticated-owner")
        assert status.phase is StorageBootstrapPhase.PROCESSING
        assert coordinator.public_status()["phase"] == "processing"
        assert coordinator.store.load().actor_user_id == "authenticated-owner"
        assert coordinator.store.load().rebuild_confirmed is True
        await coordinator.choose(StorageChoice.FRESH, "authenticated-owner")
        assert coordinator.started_tasks == 1
        await coordinator.wait()
        assert coordinator.runtime_ready()
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
async def test_bridge_explicit_fresh_survives_restart(tmp_path: Path) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    first = StorageBootstrapCoordinator(settings, sessions)
    try:
        await first.choose(StorageChoice.FRESH, "owner")
        await first.wait()
        assert json.loads(first.store.path.read_text())["rebuild_confirmed"] is True
        active = first.pointer.resolve(engine)
        restarted = StorageBootstrapCoordinator(settings, sessions, on_ready=lambda: None)
        assert (await restarted.inspect()).phase is StorageBootstrapPhase.PROCESSING
        assert restarted.public_status()["phase"] == "processing"
        await restarted.wait()
        assert restarted.runtime_ready()
        assert restarted.pointer.resolve(engine) == active
    finally:
        await first.wait()
        await db.dispose()


@pytest.mark.asyncio
async def test_bridge_corrupt_engine_never_starts_unconfirmed_fresh(tmp_path: Path) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    coordinator.store.save(
        BootstrapState(
            phase=StorageBootstrapPhase.FAILED,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=StorageChoice.FRESH,
            stage="queued",
        )
    )
    (engine / "sag.db").write_bytes(b"corrupt database")
    before = _fingerprint(engine)
    try:
        assert (await coordinator.inspect()).phase is StorageBootstrapPhase.FAILED
        assert (await coordinator.choose(StorageChoice.FRESH, "owner")).phase is StorageBootstrapPhase.FAILED
        assert coordinator.started_tasks == 0
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
async def test_bridge_confirmed_queued_rebuild_does_not_skip_work_for_current_pointer(tmp_path: Path) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    target = tmp_path / "existing-current"
    runtime = DataEngine(build_engine_config(settings, overrides={"data_dir": str(target)}), health_check=False)
    try:
        await runtime.start()
    finally:
        await runtime.aclose()
    coordinator = StorageBootstrapCoordinator(settings, sessions, on_ready=lambda: None)
    coordinator.pointer.activate(engine, target)
    coordinator.store.save(
        BootstrapState(
            phase=StorageBootstrapPhase.FAILED,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=StorageChoice.FRESH,
            actor_user_id="owner",
            stage="queued",
        )
    )
    payload = json.loads(coordinator.store.path.read_text())
    payload["rebuild_confirmed"] = True
    coordinator.store.path.write_text(json.dumps(payload))
    try:
        assert (await coordinator.inspect()).phase is StorageBootstrapPhase.PROCESSING
        await coordinator.wait()
        assert coordinator.runtime_ready()
        assert coordinator.pointer.resolve(engine) != target
    finally:
        await coordinator.wait()
        await db.dispose()
