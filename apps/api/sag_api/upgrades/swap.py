from __future__ import annotations

import os
from pathlib import Path

from sag_api.upgrades.octx_files import copy_durable_octx
from sag_api.upgrades.types import StorageUpgradeError


def _same_filesystem(*paths: Path) -> bool:
    devices = {path.parent.stat().st_dev for path in paths}
    return len(devices) == 1


def swap_engine(engine: Path, staging: Path, rollback: Path) -> None:
    if not engine.is_dir() or not staging.is_dir():
        raise StorageUpgradeError(
            "atomic engine swap requires existing source and staging directories",
            stage="swap",
            recoverable=True,
        )
    rollback.parent.mkdir(parents=True, exist_ok=True)
    if rollback.exists():
        raise StorageUpgradeError(
            "atomic engine swap rollback directory already exists",
            stage="swap",
            recoverable=True,
            backup_path=rollback,
        )
    if not _same_filesystem(engine, staging, rollback):
        raise StorageUpgradeError(
            "atomic engine swap requires one filesystem",
            stage="swap",
            recoverable=True,
        )

    preserved_octx = engine / "octx"
    if preserved_octx.is_dir():
        copy_durable_octx(preserved_octx, staging / "octx", replace=True)

    os.replace(engine, rollback)
    try:
        os.replace(staging, engine)
    except Exception as error:
        os.replace(rollback, engine)
        raise StorageUpgradeError(
            f"atomic engine swap failed: {error}",
            stage="swap",
            recoverable=True,
        ) from error


def rollback_engine(engine: Path, rollback: Path, failed_target: Path) -> None:
    if not rollback.is_dir():
        raise StorageUpgradeError(
            "rollback engine directory is missing",
            stage="rollback",
            recoverable=False,
        )
    if failed_target.exists():
        raise StorageUpgradeError(
            "failed target preservation path already exists",
            stage="rollback",
            recoverable=False,
            diagnostic_path=failed_target,
        )
    os.replace(engine, failed_target)
    try:
        os.replace(rollback, engine)
    except Exception as error:
        os.replace(failed_target, engine)
        raise StorageUpgradeError(
            f"engine rollback failed: {error}",
            stage="rollback",
            recoverable=False,
        ) from error
