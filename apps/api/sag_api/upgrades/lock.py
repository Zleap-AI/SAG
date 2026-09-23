from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout

from sag_api.upgrades.types import StorageUpgradeError


class UpgradeLock:
    def __init__(self, path: Path, *, timeout: float = 0) -> None:
        self._path = path
        self._timeout = timeout
        self._lock = FileLock(path)

    def __enter__(self) -> UpgradeLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=self._timeout)
        except Timeout as error:
            raise StorageUpgradeError(
                "another storage upgrade is already running",
                stage="lock",
                recoverable=True,
                diagnostic_path=self._path,
            ) from error
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
