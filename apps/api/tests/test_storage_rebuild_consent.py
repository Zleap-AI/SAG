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
from sag_api.upgrades.types import StorageUpgradeError


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["prompt", "windows_fresh"])
async def test_legacy_inspection_offers_only_explicit_rebuild(tmp_path: Path, policy: str) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    settings.storage_bootstrap_policy = policy
    before = _fingerprint(engine)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    try:
        status = await coordinator.inspect()
        assert status.phase is StorageBootstrapPhase.CHOICE_REQUIRED
        assert status.choices == (StorageChoice.FRESH,)
        assert coordinator.started_tasks == 0
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [StorageChoice.MIGRATE, StorageChoice.FRESH])
@pytest.mark.parametrize("phase", [StorageBootstrapPhase.PROCESSING, StorageBootstrapPhase.FAILED])
@pytest.mark.parametrize("current_pointer", [False, True])
async def test_historical_decisions_never_authorize_rebuild(
    tmp_path: Path,
    choice: StorageChoice,
    phase: StorageBootstrapPhase,
    current_pointer: bool,
) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    settings.storage_bootstrap_policy = "windows_fresh"
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    if current_pointer:
        target = tmp_path / "engine-current"
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
            choice=choice,
            actor_user_id="old-user",
            stage="processing",
        )
    )
    # An actual old state does not contain the new consent field.
    payload = json.loads(coordinator.store.path.read_text())
    payload.pop("rebuild_confirmed", None)
    coordinator.store.path.write_text(json.dumps(payload))
    before = _fingerprint(engine)
    try:
        status = await coordinator.inspect()
        assert status.phase is StorageBootstrapPhase.CHOICE_REQUIRED
        assert status.choices == (StorageChoice.FRESH,)
        assert coordinator.started_tasks == 0
        assert not coordinator.runtime_ready()
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
async def test_direct_choice_rejects_migration_without_scheduling(tmp_path: Path) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    before = _fingerprint(engine)
    try:
        with pytest.raises(StorageUpgradeError, match="fresh"):
            await coordinator.choose(StorageChoice.MIGRATE, "owner")
        assert coordinator.started_tasks == 0
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_choice", [StorageChoice.MIGRATE, StorageChoice.FRESH])
async def test_direct_explicit_rebuild_replaces_old_consent_and_persists_actor(
    tmp_path: Path,
    old_choice: StorageChoice,
) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions, on_ready=lambda: None)
    coordinator.store.save(
        BootstrapState(
            phase=StorageBootstrapPhase.FAILED,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=old_choice,
            actor_user_id="desktop-windows",
            stage="queued",
        )
    )
    try:
        status = await coordinator.choose(StorageChoice.FRESH, "authenticated-owner")
        assert status.phase is StorageBootstrapPhase.PROCESSING
        state = coordinator.store.load()
        assert state.actor_user_id == "authenticated-owner"
        assert state.rebuild_confirmed is True
        await coordinator.wait()
        assert coordinator.runtime_ready()
        assert coordinator.pointer.resolve(engine) != engine
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
async def test_ready_migrated_pointer_remains_active_without_rebuild(tmp_path: Path) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    settings.storage_bootstrap_policy = "windows_fresh"
    target = tmp_path / "engine-current"
    runtime = DataEngine(build_engine_config(settings, overrides={"data_dir": str(target)}), health_check=False)
    try:
        await runtime.start()
    finally:
        await runtime.aclose()
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    coordinator.pointer.activate(engine, target)
    coordinator.store.save(
        BootstrapState(
            phase=StorageBootstrapPhase.READY,
            source_version="legacy_0_7",
            target_version="0.8.2",
            choice=StorageChoice.MIGRATE,
            stage="ready",
        )
    )
    try:
        assert (await coordinator.inspect()).runtime_ready
        assert (await coordinator.choose(StorageChoice.FRESH, "owner")).runtime_ready
        assert coordinator.pointer.resolve(engine) == target
        assert coordinator.started_tasks == 0
    finally:
        await coordinator.wait()
        await db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_corrupt_engine_blocks_retry_without_reset(tmp_path: Path, confirmed: bool) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    state = BootstrapState(
        phase=StorageBootstrapPhase.FAILED,
        source_version="legacy_0_7",
        target_version="0.8.2",
        choice=StorageChoice.FRESH,
        stage="queued",
        rebuild_confirmed=confirmed,
    )
    coordinator.store.save(state)
    (engine / "sag.db").write_bytes(b"invalid database")
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
@pytest.mark.parametrize("pointer_payload", ["[]", "null", "missing-target"])
async def test_invalid_active_pointer_fails_closed_and_preserves_legacy(
    tmp_path: Path,
    pointer_payload: str,
) -> None:
    engine, db, sessions, settings = await _fixture(tmp_path)
    coordinator = StorageBootstrapCoordinator(settings, sessions)
    pointer = coordinator.pointer.path
    pointer.parent.mkdir(parents=True)
    if pointer_payload == "missing-target":
        coordinator.pointer.activate(engine, tmp_path / "missing-engine")
    else:
        pointer.write_text(pointer_payload)
    before = _fingerprint(engine)
    try:
        assert (await coordinator.inspect()).phase is StorageBootstrapPhase.FAILED
        assert (await coordinator.choose(StorageChoice.FRESH, "owner")).phase is StorageBootstrapPhase.FAILED
        assert coordinator.started_tasks == 0
        assert _fingerprint(engine) == before
    finally:
        await coordinator.wait()
        await db.dispose()
