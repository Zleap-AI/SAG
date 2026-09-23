from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sag_api.upgrades.backup import create_backup
from sag_api.upgrades.types import StorageLayout


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


def test_backup_skips_transient_octx_staging_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
