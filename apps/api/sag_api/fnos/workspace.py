"""Private, tenant-scoped fnOS workspace paths."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class UnsafeWorkspacePath(RuntimeError):
    """A workspace path component is not a private directory."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# fnOS grants a package user traversal permission to the volume hosting its
# app data, but not permission to list that volume. Linux O_PATH preserves the
# descriptor-relative, no-symlink traversal without requiring that listing.
_INTERMEDIATE_DIRECTORY_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW


def _validate_configured_root(path: Path) -> None:
    if ".." in path.parts:
        raise UnsafeWorkspacePath("configured workspace roots cannot contain '..'")
    if not path.parts:
        raise UnsafeWorkspacePath("configured workspace root cannot be empty")


def _open_existing_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _INTERMEDIATE_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise UnsafeWorkspacePath(f"unsafe workspace directory: {name}") from error


def _private_directory_at(parent_fd: int, name: str) -> int:
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise UnsafeWorkspacePath(f"unable to create workspace directory: {name}") from error
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise UnsafeWorkspacePath(f"unsafe workspace directory: {name}") from error
    except OSError as error:
        raise UnsafeWorkspacePath(f"unsafe workspace directory: {name}") from error

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeWorkspacePath(f"unsafe workspace directory: {name}")
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_configured_root(path: Path) -> int:
    """Open a root without following any lexical component symlink."""
    _validate_configured_root(path)
    if path.is_absolute():
        components = path.parts[1:]
    else:
        components = path.parts
    if not components:
        raise UnsafeWorkspacePath("configured workspace root cannot be the filesystem root")
    parent_fd = os.open("/" if path.is_absolute() else ".", _DIRECTORY_FLAGS)

    try:
        for name in components[:-1]:
            child_fd = _open_existing_directory(parent_fd, name)
            os.close(parent_fd)
            parent_fd = child_fd
        root_fd = _private_directory_at(parent_fd, components[-1])
    finally:
        os.close(parent_fd)
    return root_fd


def _regular_file_or_missing(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise UnsafeWorkspacePath(f"unsafe workspace file: {name}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeWorkspacePath(f"unsafe workspace file: {name}")


_TENANT_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_ISOLATION_FLAG = "SAG_FNOS_USERNAME_ISOLATION"


def username_isolation_enabled() -> bool:
    """Report whether the composite uid+username tenant key is opted in.

    Defaults to off: without the flag the layout stays byte-identical to the
    pure-UID scheme. The gateway exports its environment to every worker it
    spawns, so both sides always derive keys under the same mode.
    """
    return os.environ.get(_ISOLATION_FLAG, "").strip().lower() in {"1", "true", "on"}


def tenant_key(uid: int, username: str, *, username_isolation: bool | None = None) -> str:
    """Derive the storage key isolating one fnOS user's workspace.

    With username isolation off (the default) the key is the plain UID. When
    enabled, the username survives only as whitelist characters plus a hash
    suffix, so a reused Debian UID with a different owner never maps to the
    old key.
    """
    if type(uid) is not int or uid < 1:
        raise ValueError("fnOS UID must be a positive integer")
    if type(username) is not str:
        raise ValueError("fnOS username must be a string")
    if username_isolation is None:
        username_isolation = username_isolation_enabled()
    if not username_isolation:
        return str(uid)
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:8]
    safe = _TENANT_SAFE_RE.sub("", username)[:16]
    return f"{uid}-{safe}-{digest}"


@dataclass(frozen=True)
class WorkspacePaths:
    """All persistent and temporary locations owned by one fnOS tenant."""

    data_root: Path
    temp_root: Path
    root: Path
    meta_dir: Path
    database_file: Path
    engine_dir: Path
    uploads_dir: Path
    logs_dir: Path
    socket_file: Path

    @classmethod
    def for_identity(cls, data_root: Path, temp_root: Path, identity) -> WorkspacePaths:
        key = tenant_key(identity.uid, identity.username)
        data_root = Path(data_root)
        temp_root = Path(temp_root)
        _validate_configured_root(data_root)
        _validate_configured_root(temp_root)
        root = data_root / "users" / key
        meta_dir = root / "meta"
        return cls(
            data_root=data_root,
            temp_root=temp_root,
            root=root,
            meta_dir=meta_dir,
            database_file=meta_dir / "sag.db",
            engine_dir=root / "engine",
            uploads_dir=root / "uploads",
            logs_dir=root / "logs",
            socket_file=temp_root / "workers" / f"{key}.sock",
        )

    def prepare(self) -> None:
        """Create and validate private workspace paths through directory descriptors."""
        data_root_fd = _open_configured_root(self.data_root)
        try:
            users_fd = _private_directory_at(data_root_fd, "users")
            try:
                root_fd = _private_directory_at(users_fd, self.root.name)
                try:
                    meta_fd = _private_directory_at(root_fd, "meta")
                    try:
                        _regular_file_or_missing(meta_fd, self.database_file.name)
                    finally:
                        os.close(meta_fd)
                    for name in ("engine", "uploads", "logs"):
                        directory_fd = _private_directory_at(root_fd, name)
                        os.close(directory_fd)
                finally:
                    os.close(root_fd)
            finally:
                os.close(users_fd)
        finally:
            os.close(data_root_fd)

        temp_root_fd = _open_configured_root(self.temp_root)
        try:
            workers_fd = _private_directory_at(temp_root_fd, "workers")
            os.close(workers_fd)
        finally:
            os.close(temp_root_fd)
