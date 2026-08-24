from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def _ignore_transient_staging(source: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory) == source and "staging" in names:
            return {"staging"}
        return set()

    return ignore


def _remove_readonly_tree(path: Path) -> None:
    def retry_with_write_access(function, blocked_path, _error) -> None:
        os.chmod(blocked_path, stat.S_IWRITE)
        function(blocked_path)

    shutil.rmtree(path, onerror=retry_with_write_access)


def copy_durable_octx(source: Path, destination: Path, *, replace: bool = False) -> None:
    """Copy persistent OCTX state without transient transfer workspaces or file metadata."""
    if not source.is_dir():
        return
    if replace and destination.exists():
        _remove_readonly_tree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=_ignore_transient_staging(source),
        copy_function=shutil.copyfile,
    )
