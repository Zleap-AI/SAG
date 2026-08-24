from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sag_api.upgrades.octx_files import copy_durable_octx
from sag_api.upgrades.types import StorageLayout, StorageUpgradeError


@dataclass(frozen=True)
class BackupManifest:
    migration_id: str
    source_version: str
    backup_root: Path
    engine_path: Path
    engine_sha256: str
    engine_size: int
    sag_db: Path | None
    sag_db_sha256: str | None
    sag_db_size: int
    created_at: str
    manifest_path: Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_stats(root: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_size = path.stat().st_size
        size += file_size
        digest.update(file_size.to_bytes(8, "big"))
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    return size, digest.hexdigest()


def _source_size(layout: StorageLayout) -> int:
    engine_size, _ = _tree_stats(layout.engine)
    sag_size = 0
    if layout.sag_db is not None and layout.sag_db.is_file():
        sag_size = layout.sag_db.stat().st_size
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{layout.sag_db}{suffix}")
            if companion.is_file():
                sag_size += companion.stat().st_size
    return engine_size + sag_size


def _preflight_space(layout: StorageLayout) -> None:
    required = int(_source_size(layout) * 2.2)
    free = shutil.disk_usage(layout.root).free
    if free < required:
        raise StorageUpgradeError(
            f"insufficient free disk space for storage backup: need {required}, have {free}",
            stage="backup",
            recoverable=True,
        )


def create_sqlite_backup(source_path: Path, destination_path: Path) -> None:
    """Copy a SQLite database through its online backup API."""
    source_uri = f"file:{source_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source:
        with sqlite3.connect(destination_path) as destination:
            source.backup(destination)


def _load_manifest(path: Path) -> BackupManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent
    sag_relative = payload.get("sag_db")
    return BackupManifest(
        migration_id=str(payload["migration_id"]),
        source_version=str(payload["source_version"]),
        backup_root=root,
        engine_path=root / str(payload["engine_path"]),
        engine_sha256=str(payload["engine_sha256"]),
        engine_size=int(payload["engine_size"]),
        sag_db=root / str(sag_relative) if sag_relative else None,
        sag_db_sha256=payload.get("sag_db_sha256"),
        sag_db_size=int(payload.get("sag_db_size", 0)),
        created_at=str(payload["created_at"]),
        manifest_path=path,
    )


def create_backup(layout: StorageLayout, migration_id: str, *, source_version: str) -> BackupManifest:
    destination = layout.backups / migration_id
    manifest_path = destination / "manifest.json"
    if manifest_path.is_file():
        manifest = _load_manifest(manifest_path)
        engine_size, engine_sha256 = _tree_stats(manifest.engine_path)
        if (engine_size, engine_sha256) != (
            manifest.engine_size,
            manifest.engine_sha256,
        ):
            raise StorageUpgradeError(
                "existing storage backup failed checksum validation",
                stage="backup",
                recoverable=False,
                backup_path=destination,
                diagnostic_path=manifest_path,
            )
        return manifest
    if destination.exists():
        raise StorageUpgradeError(
            "incomplete storage backup already exists",
            stage="backup",
            recoverable=True,
            backup_path=destination,
        )

    _preflight_space(layout)
    layout.backups.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{migration_id}.", dir=layout.backups))
    try:
        engine_copy = temporary / "engine"
        engine_octx = layout.engine / "octx"

        def ignore_octx_root(directory: str, names: list[str]) -> set[str]:
            if Path(directory) == layout.engine and "octx" in names:
                return {"octx"}
            return set()

        # Backup data bytes only. copy2 also replays source metadata, which can
        # turn OCTX artifacts into Windows read-only files and fail with
        # WinError 5 while copying or cleaning an interrupted backup.
        shutil.copytree(
            layout.engine,
            engine_copy,
            ignore=ignore_octx_root,
            copy_function=shutil.copyfile,
        )
        copy_durable_octx(engine_octx, engine_copy / "octx")
        engine_size, engine_sha256 = _tree_stats(engine_copy)

        sag_copy: Path | None = None
        sag_sha256: str | None = None
        sag_size = 0
        if layout.sag_db is not None and layout.sag_db.is_file():
            sag_copy = temporary / "sag.db"
            create_sqlite_backup(layout.sag_db, sag_copy)
            sag_size = sag_copy.stat().st_size
            sag_sha256 = _sha256_file(sag_copy)

        created_at = datetime.now(UTC).isoformat()
        payload = {
            "migration_id": migration_id,
            "source_version": source_version,
            "engine_path": "engine",
            "engine_sha256": engine_sha256,
            "engine_size": engine_size,
            "sag_db": "sag.db" if sag_copy else None,
            "sag_db_sha256": sag_sha256,
            "sag_db_size": sag_size,
            "created_at": created_at,
        }
        draft_manifest = temporary / "manifest.json"
        draft_manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with draft_manifest.open("rb") as source:
            os.fsync(source.fileno())
        os.replace(temporary, destination)
        return _load_manifest(manifest_path)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def prepare_staging(layout: StorageLayout, migration_id: str) -> Path:
    engine = layout.staging / migration_id / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    return engine
