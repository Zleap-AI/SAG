#!/usr/bin/env python3

import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile


DATA_ROOT = Path(os.environ.get("SAG_DATA_ROOT", "/data"))
BACKUP_ROOT = Path(os.environ.get("SAG_BACKUP_ROOT", "/backup"))
CONFIG_ROOT = Path(os.environ.get("SAG_CONFIG_ROOT", "/config"))


def data_size_kib() -> None:
    total = 0
    for root, directories, files in os.walk(DATA_ROOT, followlinks=False):
        root_stat = os.lstat(root)
        total += max(root_stat.st_size, root_stat.st_blocks * 512)
        for name in directories:
            path = Path(root, name)
            if path.is_symlink():
                item_stat = os.lstat(path)
                total += max(item_stat.st_size, item_stat.st_blocks * 512)
        for name in files:
            try:
                item_stat = os.lstat(Path(root, name))
                total += max(item_stat.st_size, item_stat.st_blocks * 512)
            except FileNotFoundError:
                pass
    print((total + 1023) // 1024)


def create_backup() -> None:
    requested = PurePosixPath(os.environ["SAG_ARCHIVE_TEMP"])
    if requested.parent != PurePosixPath("/backup") or requested.name in {"", ".", ".."}:
        raise ValueError("SAG_ARCHIVE_TEMP must name one file directly under /backup")
    target = BACKUP_ROOT / requested.name
    reserved = False
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        reserved = True
        with tarfile.open(target, "w:gz") as archive:
            archive.dereference = False
            archive.add(DATA_ROOT, arcname="data", recursive=True)
        target.chmod(0o600)
    except BaseException:
        if reserved:
            target.unlink(missing_ok=True)
        raise


def delete_data_contents() -> None:
    for entry in os.scandir(DATA_ROOT):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def main() -> int:
    os.umask(0o077)
    action = os.environ.get("SAG_LIFECYCLE_ACTION")
    actions = {
        "size": data_size_kib,
        "backup": create_backup,
        "delete": delete_data_contents,
    }
    if action not in actions:
        print("unsupported SAG_LIFECYCLE_ACTION", file=sys.stderr)
        return 2
    actions[action]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
