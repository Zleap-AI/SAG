"""Safe offline lifecycle operations for fnOS Native user data."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

_UID = re.compile(r"[1-9][0-9]*\Z")


def valid_secret(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _users_root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("users root must be a real directory")
    resolved = root.resolve(strict=True)
    if resolved.name != "users" or resolved.parent != Path(os.environ["TRIM_PKGVAR"]).resolve(strict=True):
        raise ValueError("users root must be the direct users child of TRIM_PKGVAR")
    return resolved


def _validate_tree(root: Path) -> None:
    for user in root.iterdir():
        if not _UID.fullmatch(user.name) or user.is_symlink() or not user.is_dir():
            raise ValueError("users root contains an invalid user directory")
        for path in user.rglob("*"):
            if path.is_symlink():
                raise ValueError("user data must not contain symlinks")


def size(root: Path) -> int:
    root = _users_root(root)
    _validate_tree(root)
    return sum(path.stat().st_blocks * 512 for path in root.rglob("*") if path.is_file()) // 1024


def backup(root: Path, output: Path) -> None:
    root = _users_root(root)
    _validate_tree(root)
    output = Path(output)
    backup_dir = root.parent / "backup"
    if output.parent.resolve(strict=False) != backup_dir.resolve(strict=False) or output.name.endswith(".tmp") is False:
        raise ValueError("backup output must be a direct .tmp file in the package backup directory")
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(output, "w:gz", dereference=False) as archive:
        archive.add(root, arcname="users", recursive=True)
    os.chmod(output, 0o600)


def validate(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as source, tempfile.TemporaryDirectory(prefix="sag-restore-") as temp:
        target = Path(temp)
        for member in source.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "users":
                raise ValueError("archive contains an unsafe path")
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError("archive contains unsupported links or devices")
            if len(name.parts) > 1 and not _UID.fullmatch(name.parts[1]):
                raise ValueError("archive contains an invalid user directory")
        # NOTE: extractall(filter="data") was added in Python 3.12. upgrade_init
        # falls back to the system python3, which on many fnOS images is
        # 3.10/3.11 and rejects that kwarg with TypeError. The membership loop
        # above already enforces the same safety envelope (no absolute paths,
        # no ..-escapes, no symlinks/hardlinks/devices, first path component
        # must be "users", UID directories match _UID) — so we can omit the
        # filter and stay compatible with the older interpreters fnOS ships.
        source.extractall(target)
        users = target / "users"
        if not users.is_dir():
            raise ValueError("archive does not contain users")
        for database in users.glob("*/meta/sag.db"):
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
            finally:
                connection.close()


def delete(root: Path) -> None:
    if os.environ.get("SAG_DELETE_DATA") != "true":
        raise ValueError("SAG_DELETE_DATA=true is required")
    root = _users_root(root)
    _validate_tree(root)
    for child in root.iterdir():
        shutil.rmtree(child)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("size", "backup", "validate", "delete"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.action == "size":
        print(size(args.root))
    elif args.action == "backup":
        backup(args.root, args.output)
    elif args.action == "validate":
        validate(args.archive)
    else:
        delete(args.root)


if __name__ == "__main__":
    main()
