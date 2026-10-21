"""Complete the confirmed native package upgrade while SAG is stopped."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import fcntl
import importlib.util
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sag_api.fnos.knowledge_compatibility import classify_tenant, classify_users


class PackageUpgradeInterrupted(BaseException):
    """Cancellation must not be swallowed as an OS-level EINTR by subprocess."""


def _interrupted(signum, frame):
    raise PackageUpgradeInterrupted("package upgrade interrupted")


def _bind_parent() -> None:
    """An interrupted installer must not leave an independent metadata writer."""
    signal.signal(signal.SIGTERM, _interrupted)
    if sys.platform == "linux":
        parent = os.getppid()
        if parent <= 1 or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError("package upgrade requires a live installer parent")
        if os.getppid() != parent:
            raise InterruptedError("installer parent exited")


def _require_context(root: Path) -> None:
    if os.environ.get("TRIM_APP_STATUS") != "UPGRADE":
        raise ValueError("knowledge rebuilding is only available during package upgrade")
    if root != Path(os.environ["TRIM_PKGVAR"]).resolve(strict=True):
        raise ValueError("package data root differs")
    # cmd/main holds this same open file description through stop, backup and
    # all child migrations. A second start cannot enter between those steps.
    held = os.fstat(9)
    expected = (root / "run/lifecycle.lock").stat(follow_symlinks=False)
    if not stat.S_ISREG(expected.st_mode) or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("package lifecycle lock is required")
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _cold_backup(root: Path, tool: Path) -> None:
    spec = importlib.util.spec_from_file_location("sag_package_lifecycle", tool)
    if spec is None or spec.loader is None:
        raise ValueError("package backup tool is unavailable")
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)
    directory = root / "backup"
    if directory.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    temporary = directory / f"sag-upgrade-{uuid4().hex}.tar.gz.tmp"
    try:
        lifecycle.backup(root / "users", temporary)
        lifecycle.validate(temporary)
        with temporary.open("rb") as archive:
            os.fsync(archive.fileno())
        temporary.rename(temporary.with_suffix(""))
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


async def _upgrade_tenant(workspace: Path) -> None:
    # Configure the Settings singleton before importing the database module.
    os.environ.update({
        "SAG_AUTH_MODE": "fnos",
        "SAG_DATABASE_URL": f"sqlite+aiosqlite:////{workspace / 'meta/sag.db'}",
        "SAG_DATA_DIR": str(workspace / "engine-v0.13-clean"),
        "SAG_UPLOAD_DIR": str(workspace / "uploads"),
    })
    from sag_api.core.db import SessionLocal, dispose_db, init_db
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    try:
        await init_db()
        async with SessionLocal() as session:
            await reset_legacy_knowledge(session, workspace / "engine", allow_legacy_reset=True)
    finally:
        await dispose_db()


def upgrade_users(root: Path, tool: Path) -> int:
    users = root / "users"
    if not users.exists():
        return 0
    # Validate every tenant before changing any one of them.
    classify_users(users)
    workspaces = [path for path in sorted(users.iterdir()) if classify_tenant(path) == "legacy"]
    _cold_backup(root, tool)
    if not workspaces:
        return 0
    with TemporaryDirectory(prefix="sag-upgrade-meta-", dir=root / "backup") as temporary:
        saved = Path(temporary)
        for workspace in workspaces:
            shutil.copytree(workspace / "meta", saved / workspace.name)
        started = []
        try:
            for workspace in workspaces:
                started.append(workspace)
                subprocess.run(
                    [sys.executable, "-m", __spec__.name, "--root", str(root), "--tenant", workspace.name],
                    check=True, capture_output=True, pass_fds=(9,),
                )
        except BaseException:
            # Child termination/reaping precedes this handler. Restore complete
            # cold metadata, including WAL/SHM, if any tenant failed.
            for workspace in started:
                workspace.joinpath("meta").rename(saved / f"failed-{workspace.name}")
                saved.joinpath(workspace.name).rename(workspace / "meta")
            raise
    return len(workspaces)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup-tool", type=Path)
    parser.add_argument("--tenant")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        _require_context(root)
        _bind_parent()
        if args.tenant is not None:
            if args.tenant not in {path.name for path in (root / "users").iterdir()}:
                raise ValueError("unknown tenant")
            workspace = root / "users" / args.tenant
            if classify_tenant(workspace) != "legacy":
                raise ValueError("only recognized legacy knowledge can be rebuilt")
            asyncio.run(_upgrade_tenant(workspace))
        else:
            if args.backup_tool is None:
                raise ValueError("package backup tool is required")
            count = upgrade_users(root, args.backup_tool)
            print(f"knowledge upgrade complete: {count} rebuilt")
        return 0
    except (Exception, PackageUpgradeInterrupted) as error:
        print(f"知识库更新未完成。请保留数据和已有备份，重试或联系维护者。({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
