from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sag_api.core.config import Settings
from sag_api.upgrades.backup import create_backup
from sag_api.upgrades.contracts import StorageUpgradeContext
from sag_api.upgrades.contracts import UpgradeReport as StorageMigrationResult
from sag_api.upgrades.journal import MigrationJournal
from sag_api.upgrades.swap import swap_engine
from sag_api.upgrades.types import MigrationPhase, StorageLayout, StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.adapter import ZleapSag071To082Adapter
from sag_api.upgrades.zleap_sag_0_7_to_0_8.migrator import MIGRATION_ID


def test_backup_copies_octx_payload_without_windows_restricted_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    artifact = engine / "octx" / "releases" / "asset" / "1.0.0" / "package.octx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing-package")
    layout = StorageLayout(
        root=tmp_path,
        engine=engine,
        sag_db=None,
        upgrades=tmp_path / ".storage-upgrades",
        backups=tmp_path / ".storage-upgrades" / "backups",
        staging=tmp_path / ".storage-upgrades" / "staging",
    )
    original_copystat = shutil.copystat

    def reject_octx_metadata(source, destination, *, follow_symlinks=True):
        if Path(source).suffix == ".octx":
            raise PermissionError(5, "Access is denied", destination)
        return original_copystat(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copystat", reject_octx_metadata)

    backup = create_backup(layout, "migration", source_version="0.7.1")

    assert (backup.engine_path / artifact.relative_to(engine)).read_bytes() == b"existing-package"


def test_backup_skips_transient_octx_staging_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    release = engine / "octx" / "releases" / "asset" / "1.0.0" / "package.octx"
    workspace = engine / "octx" / "workspaces" / "source" / ".octx" / "state.json"
    transient = engine / "octx" / "staging" / "transfer" / "export-1" / "workspace" / "document.md"
    for path, payload in (
        (release, b"existing-package"),
        (workspace, b"persistent-state"),
        (transient, b"temporary-export"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    layout = StorageLayout(
        root=tmp_path,
        engine=engine,
        sag_db=None,
        upgrades=tmp_path / ".storage-upgrades",
        backups=tmp_path / ".storage-upgrades" / "backups",
        staging=tmp_path / ".storage-upgrades" / "staging",
    )
    original_copyfile = shutil.copyfile

    def reject_transient_staging(source, destination, *, follow_symlinks=True):
        if Path(source).is_relative_to(engine / "octx" / "staging"):
            raise FileNotFoundError(2, "No such file or directory", destination)
        return original_copyfile(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", reject_transient_staging)

    backup = create_backup(layout, "migration", source_version="0.7.1")

    assert (backup.engine_path / release.relative_to(engine)).read_bytes() == b"existing-package"
    assert (backup.engine_path / workspace.relative_to(engine)).read_bytes() == b"persistent-state"
    assert not (backup.engine_path / "octx" / "staging").exists()


def test_swap_restores_original_when_second_rename_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = tmp_path / "engine"
    staging = tmp_path / "staging"
    rollback = tmp_path / "rollback"
    engine.mkdir()
    staging.mkdir()
    (engine / "marker").write_text("legacy", encoding="utf-8")
    (staging / "marker").write_text("current", encoding="utf-8")

    from sag_api.upgrades import swap as swap_module

    original_replace = swap_module.os.replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected swap failure")
        original_replace(source, destination)

    monkeypatch.setattr(swap_module.os, "replace", fail_second)
    with pytest.raises(StorageUpgradeError, match="atomic engine swap"):
        swap_engine(engine, staging, rollback)

    assert (engine / "marker").read_text(encoding="utf-8") == "legacy"
    assert (staging / "marker").read_text(encoding="utf-8") == "current"
    assert not rollback.exists()


def test_swap_preserves_octx_artifacts_in_current_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine schema swap must not move application-level OCTX releases offline."""
    engine = tmp_path / "engine"
    staging = tmp_path / "staging"
    rollback = tmp_path / "rollback"
    artifact = engine / "octx" / "releases" / "asset" / "1.0.0" / "package.octx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing-package")
    staging.mkdir()
    (staging / "marker").write_text("current", encoding="utf-8")
    original_copystat = shutil.copystat

    def reject_octx_metadata(source, destination, *, follow_symlinks=True):
        if Path(source).suffix == ".octx":
            raise PermissionError(5, "Access is denied", destination)
        return original_copystat(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copystat", reject_octx_metadata)

    swap_engine(engine, staging, rollback)

    assert (engine / "octx" / artifact.relative_to(engine / "octx")).read_bytes() == b"existing-package"
    assert (rollback / "octx" / artifact.relative_to(engine / "octx")).read_bytes() == b"existing-package"


def test_swap_replaces_octx_left_by_an_interrupted_windows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    staging = tmp_path / "staging"
    rollback = tmp_path / "rollback"
    artifact = engine / "octx" / "releases" / "asset" / "1.0.0" / "package.octx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing-package")
    stale = staging / artifact.relative_to(engine)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial-package")
    (staging / "marker").write_text("current", encoding="utf-8")
    original_copyfile = shutil.copyfile

    def reject_existing_destination(source, destination, *, follow_symlinks=True):
        if Path(destination).exists():
            raise PermissionError(5, "Access is denied", destination)
        return original_copyfile(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", reject_existing_destination)

    swap_engine(engine, staging, rollback)

    assert artifact.read_bytes() == b"existing-package"
    assert (engine / "marker").read_text(encoding="utf-8") == "current"


@pytest.mark.asyncio
async def test_verified_phase_completes_swap_after_process_dies_between_renames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=str(tmp_path / "engine"),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}",
        _env_file=None,
    )
    layout = StorageLayout.from_settings(settings)
    state_root = layout.upgrades / MIGRATION_ID
    rollback = state_root / "original-engine"
    staging = layout.staging / MIGRATION_ID / "engine"
    rollback.mkdir(parents=True)
    staging.mkdir(parents=True)
    (rollback / "marker").write_text("legacy", encoding="utf-8")
    (staging / "marker").write_text("current", encoding="utf-8")

    journal = MigrationJournal.create(state_root / "journal.json", migration_id=MIGRATION_ID)
    for phase in (
        MigrationPhase.BACKED_UP,
        MigrationPhase.RELATIONAL_MIGRATED,
        MigrationPhase.VECTORS_MIGRATED,
        MigrationPhase.CHECKPOINTS_MIGRATED,
        MigrationPhase.VERIFIED,
    ):
        journal.advance(phase)

    async def finish(_settings, _session_factory, recovered_layout, recovered_journal):
        assert recovered_journal.phase is MigrationPhase.SWAPPED
        assert (recovered_layout.engine / "marker").read_text(encoding="utf-8") == "current"
        return StorageMigrationResult("migrated", recovered_journal.path, rollback)

    monkeypatch.setattr("sag_api.upgrades.zleap_sag_0_7_to_0_8.migrator._finish_swapped_checkpoint", finish)

    result = await ZleapSag071To082Adapter().migrate(
        StorageUpgradeContext(settings=settings, session_factory=None)
    )

    assert result.status == "migrated"
    assert (rollback / "marker").read_text(encoding="utf-8") == "legacy"
