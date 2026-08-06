from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from sag_api.fnos import workspace
from sag_api.fnos.identity import GatewayIdentity
from sag_api.fnos.workspace import (
    UnsafeWorkspacePath,
    WorkspacePaths,
    tenant_key,
    username_isolation_enabled,
)

KEY_ALICE = "1000-Alice-3bc51062"


def _paths(data_root: Path, temp_root: Path) -> WorkspacePaths:
    return WorkspacePaths.for_identity(data_root, temp_root, GatewayIdentity(1000, "Alice", False))


def test_tenant_key_defaults_to_pure_uid_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches the composite key leaking into deployments that did not opt in."""
    monkeypatch.delenv("SAG_FNOS_USERNAME_ISOLATION", raising=False)
    assert tenant_key(1000, "Alice") == "1000"
    assert tenant_key(1000, "") == "1000"
    assert tenant_key(1000, "张三") == "1000"


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("1", True), ("true", True), ("TRUE", True), ("on", True), (" On ", True),
     ("0", False), ("false", False), ("off", False), ("", False), ("yes", False)],
)
def test_username_isolation_flag_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, enabled: bool
) -> None:
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", value)
    assert username_isolation_enabled() is enabled
    assert tenant_key(1000, "Alice") == (KEY_ALICE if enabled else "1000")


@pytest.mark.parametrize(
    ("uid", "username", "expected"),
    [
        (1000, "Alice", "1000-Alice-3bc51062"),
        (1000, "admin", "1000-admin-8c6976e5"),
        (1000, "", "1000--e3b0c442"),
        (1000, "张三", "1000--1d841bc0"),
        (1000, "user.1", "1000-user1-6a9dfbe2"),
        (1000, "a" * 120, f"1000-{'a' * 16}-2f3d3354"),
    ],
)
def test_tenant_key_composite_derivation(uid: int, username: str, expected: str) -> None:
    """Catches special characters or unbounded usernames reaching storage keys."""
    key = tenant_key(uid, username, username_isolation=True)
    assert key == expected
    assert re.fullmatch(r"[0-9]+-[a-zA-Z0-9_-]{0,16}-[0-9a-f]{8}", key)


def test_tenant_key_separates_same_uid_and_whitelist_collisions() -> None:
    """Catches a reused Debian UID inheriting the previous owner's workspace."""
    assert tenant_key(1000, "Alice", username_isolation=True) != tenant_key(1000, "Bob", username_isolation=True)
    assert tenant_key(1000, "user.1", username_isolation=True) != tenant_key(1000, "user_1", username_isolation=True)


def test_tenant_key_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        tenant_key(0, "Alice")
    with pytest.raises(ValueError):
        tenant_key("1000", "Alice")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        tenant_key(1000, None)  # type: ignore[arg-type]


def test_workspace_layout_is_uid_scoped_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a layout change for deployments that did not opt into username isolation."""
    monkeypatch.delenv("SAG_FNOS_USERNAME_ISOLATION", raising=False)
    paths = _paths(tmp_path / "data", tmp_path / "tmp")

    assert paths.root == tmp_path / "data/users/1000"
    assert paths.database_file == tmp_path / "data/users/1000/meta/sag.db"
    assert paths.socket_file == tmp_path / "tmp/workers/1000.sock"


def test_workspace_layout_is_tenant_key_scoped_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches persistent user data or the worker socket being shared across tenants."""
    monkeypatch.setenv("SAG_FNOS_USERNAME_ISOLATION", "1")
    paths = _paths(tmp_path / "data", tmp_path / "tmp")

    assert paths.root == tmp_path / f"data/users/{KEY_ALICE}"
    assert paths.meta_dir == tmp_path / f"data/users/{KEY_ALICE}/meta"
    assert paths.database_file == tmp_path / f"data/users/{KEY_ALICE}/meta/sag.db"
    assert paths.engine_dir == tmp_path / f"data/users/{KEY_ALICE}/engine"
    assert paths.uploads_dir == tmp_path / f"data/users/{KEY_ALICE}/uploads"
    assert paths.logs_dir == tmp_path / f"data/users/{KEY_ALICE}/logs"
    assert paths.socket_file == tmp_path / f"tmp/workers/{KEY_ALICE}.sock"


def test_prepare_creates_private_workspace_directories(tmp_path: Path) -> None:
    """Catches workspace state being created with group- or world-readable permissions."""
    paths = _paths(tmp_path / "data", tmp_path / "tmp")

    paths.prepare()

    for directory in (
        tmp_path / "data",
        tmp_path / "data/users",
        paths.root,
        paths.meta_dir,
        paths.engine_dir,
        paths.uploads_dir,
        paths.logs_dir,
        tmp_path / "tmp",
        tmp_path / "tmp/workers",
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_workspace_uses_path_only_descriptors_for_intermediate_directories() -> None:
    """fnOS grants package data access without granting read access to volume roots."""
    assert workspace._INTERMEDIATE_DIRECTORY_FLAGS & getattr(os, "O_PATH", 0) == getattr(os, "O_PATH", 0)


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Linux-only descriptor access contract")
def test_prepare_crosses_execute_only_volume_root(tmp_path: Path) -> None:
    """Opening an app root must not require directory listing access on its volume."""
    volume = tmp_path / "volume"
    data_root = volume / "appdata" / "sag"
    data_root.mkdir(parents=True)
    volume.chmod(0o111)
    try:
        _paths(data_root, tmp_path / "tmp").prepare()
    finally:
        volume.chmod(0o700)

    assert (data_root / "users/1000/meta").is_dir()


def test_prepare_rejects_symlinked_user_root(tmp_path: Path) -> None:
    """Catches a user workspace redirecting writes outside its UID-scoped root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data/users").mkdir(parents=True)
    (tmp_path / "data/users/1000").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeWorkspacePath):
        _paths(tmp_path / "data", tmp_path / "tmp").prepare()


def test_prepare_rejects_non_directory_or_symlinked_workspace_component(tmp_path: Path) -> None:
    """Catches a file or redirected child directory being treated as trusted workspace state."""
    paths = _paths(tmp_path / "data", tmp_path / "tmp")
    paths.root.mkdir(parents=True)
    paths.engine_dir.symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(UnsafeWorkspacePath):
        paths.prepare()

    paths.engine_dir.unlink()
    paths.meta_dir.rmdir()
    paths.meta_dir.write_text("not a directory")

    with pytest.raises(UnsafeWorkspacePath):
        paths.prepare()


@pytest.mark.parametrize("root_name", ["data_root", "temp_root"])
def test_for_identity_rejects_configured_root_path_traversal(tmp_path: Path, root_name: str) -> None:
    """Catches a configured root escaping its package-owned directory with `..`."""
    data_root = tmp_path / "data"
    temp_root = tmp_path / "tmp"
    if root_name == "data_root":
        data_root = tmp_path / "data/../outside"
    else:
        temp_root = tmp_path / "tmp/../outside"

    with pytest.raises(UnsafeWorkspacePath):
        _paths(data_root, temp_root)


@pytest.mark.parametrize("root_name", ["data_root", "temp_root"])
def test_prepare_rejects_intermediate_symlink_in_configured_root(
    tmp_path: Path, root_name: str
) -> None:
    """Catches a configured data or temporary root that crosses a symlinked prefix."""
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    data_root = tmp_path / "data"
    temp_root = tmp_path / "tmp"
    if root_name == "data_root":
        data_root = alias / "data"
    else:
        temp_root = alias / "tmp"

    with pytest.raises(UnsafeWorkspacePath):
        _paths(data_root, temp_root).prepare()


def test_prepare_does_not_follow_a_root_replaced_during_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a root swapped to a symlink after it is checked but before descendants are made."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original_root = tmp_path / "data-before-replacement"
    replaced = False

    def replace_root() -> None:
        nonlocal replaced
        if replaced:
            return
        replaced = True
        data_root.rename(original_root)
        data_root.symlink_to(outside, target_is_directory=True)

    path_chmod = Path.chmod

    def replace_before_path_chmod(path: Path, mode: int, *args: object, **kwargs: object) -> None:
        if path == data_root:
            replace_root()
        path_chmod(path, mode, *args, **kwargs)

    fchmod = os.fchmod

    def replace_before_fchmod(fd: int, mode: int) -> None:
        replace_root()
        fchmod(fd, mode)

    monkeypatch.setattr(Path, "chmod", replace_before_path_chmod)
    monkeypatch.setattr(os, "fchmod", replace_before_fchmod)

    _paths(data_root, tmp_path / "tmp").prepare()

    assert replaced
    assert not (outside / "users").exists()
    assert (original_root / "users/1000").is_dir()
