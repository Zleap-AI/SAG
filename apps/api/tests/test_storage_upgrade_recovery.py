from __future__ import annotations

import errno
import os
import shutil
import time
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


class WindowsAccessDenied(PermissionError):
    winerror = 5


def test_backup_retries_transient_windows_directory_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "payload.bin").write_bytes(b"legacy-data")
    layout = StorageLayout(
        root=tmp_path,
        engine=engine,
        sag_db=None,
        upgrades=tmp_path / ".storage-upgrades",
        backups=tmp_path / ".storage-upgrades" / "backups",
        staging=tmp_path / ".storage-upgrades" / "staging",
    )
    original_replace = os.replace
    attempts = 0

    def transient_windows_lock(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise WindowsAccessDenied(errno.EACCES, "Access is denied", source, None, destination)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", transient_windows_lock)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    backup = create_backup(layout, "migration", source_version="0.7.1")

    assert backup.manifest_path.is_file()
    assert attempts == 3


def test_backup_reuses_completed_temporary_directory_after_windows_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "payload.bin").write_bytes(b"legacy-data")
    layout = StorageLayout(
        root=tmp_path,
        engine=engine,
        sag_db=None,
        upgrades=tmp_path / ".storage-upgrades",
        backups=tmp_path / ".storage-upgrades" / "backups",
        staging=tmp_path / ".storage-upgrades" / "staging",
    )
    original_replace = os.replace

    def persistent_windows_lock(source: Path, destination: Path) -> None:
        raise WindowsAccessDenied(errno.EACCES, "Access is denied", source, None, destination)

    monkeypatch.setattr(os, "replace", persistent_windows_lock)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    with pytest.raises(WindowsAccessDenied):
        create_backup(layout, "migration", source_version="0.7.1")

    completed = list(layout.backups.glob(".migration.*/manifest.json"))
    assert len(completed) == 1

    monkeypatch.setattr(os, "replace", original_replace)

    def reject_duplicate_copy(*_args, **_kwargs):
        raise AssertionError("completed backup must be promoted without copying again")

    monkeypatch.setattr(shutil, "copytree", reject_duplicate_copy)

    backup = create_backup(layout, "migration", source_version="0.7.1")

    assert backup.manifest_path.is_file()
    assert not completed[0].exists()


def test_backup_fsync_uses_windows_compatible_writable_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "sag.db").write_bytes(b"engine-data")
    layout = StorageLayout(
        root=tmp_path,
        engine=engine,
        sag_db=None,
        upgrades=tmp_path / ".storage-upgrades",
        backups=tmp_path / ".storage-upgrades" / "backups",
        staging=tmp_path / ".storage-upgrades" / "staging",
    )
    original_open = Path.open
    original_fsync = os.fsync
    modes_by_descriptor: dict[int, str] = {}

    def track_open(path: Path, mode: str = "r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        modes_by_descriptor[handle.fileno()] = mode
        return handle

    def reject_read_only_fsync(descriptor: int) -> None:
        mode = modes_by_descriptor.get(descriptor, "")
        if "r" in mode and "+" not in mode:
            raise OSError(errno.EBADF, "Bad file descriptor")
        original_fsync(descriptor)

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(os, "fsync", reject_read_only_fsync)

    backup = create_backup(layout, "migration", source_version="0.7.1")

    assert backup.manifest_path.is_file()


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

    from sag_api.upgrades import directory_replace as directory_replace_module

    original_replace = directory_replace_module.os.replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected swap failure")
        original_replace(source, destination)

    monkeypatch.setattr(directory_replace_module.os, "replace", fail_second)
    with pytest.raises(StorageUpgradeError, match="atomic engine swap"):
        swap_engine(engine, staging, rollback)

    assert (engine / "marker").read_text(encoding="utf-8") == "legacy"
    assert (staging / "marker").read_text(encoding="utf-8") == "current"
    assert not rollback.exists()


def test_swap_retries_transient_windows_directory_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = tmp_path / "engine"
    staging = tmp_path / "staging"
    rollback = tmp_path / "rollback"
    engine.mkdir()
    staging.mkdir()
    (engine / "marker").write_text("legacy", encoding="utf-8")
    (staging / "marker").write_text("current", encoding="utf-8")
    original_replace = os.replace
    attempts: dict[tuple[Path, Path], int] = {}

    def transient_windows_lock(source: Path, destination: Path) -> None:
        key = (Path(source), Path(destination))
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            raise WindowsAccessDenied(errno.EACCES, "Access is denied", source, None, destination)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", transient_windows_lock)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    swap_engine(engine, staging, rollback)

    assert (engine / "marker").read_text(encoding="utf-8") == "current"
    assert (rollback / "marker").read_text(encoding="utf-8") == "legacy"
    assert attempts[(engine, rollback)] == 2
    assert attempts[(staging, engine)] == 2


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
    original_replace = os.replace
    recovery_attempts = 0

    def transient_windows_lock(source: Path, destination: Path) -> None:
        nonlocal recovery_attempts
        if Path(source) == staging and Path(destination) == layout.engine:
            recovery_attempts += 1
            if recovery_attempts == 1:
                raise WindowsAccessDenied(errno.EACCES, "Access is denied", source, None, destination)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", transient_windows_lock)
    monkeypatch.setattr(time, "sleep", lambda _delay: None)

    result = await ZleapSag071To082Adapter().migrate(
        StorageUpgradeContext(settings=settings, session_factory=None)
    )

    assert result.status == "migrated"
    assert recovery_attempts == 2
    assert (rollback / "marker").read_text(encoding="utf-8") == "legacy"
