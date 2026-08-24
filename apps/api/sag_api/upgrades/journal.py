from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from sag_api.upgrades.types import MIGRATION_PHASES, MigrationPhase, StorageUpgradeError


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class MigrationJournal:
    path: Path
    migration_id: str
    phase: MigrationPhase
    created_at: str
    updated_at: str
    reports: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, path: Path, *, migration_id: str) -> MigrationJournal:
        now = _now()
        journal = cls(path, migration_id, MigrationPhase.DETECTED, now, now)
        journal._write()
        return journal

    @classmethod
    def load(cls, path: Path) -> MigrationJournal:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            migration_id=str(payload["migration_id"]),
            phase=MigrationPhase(payload["phase"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            reports=dict(payload.get("reports", {})),
        )

    def advance(self, phase: MigrationPhase, *, report: Any | None = None) -> None:
        current_index = MIGRATION_PHASES.index(self.phase)
        target_index = MIGRATION_PHASES.index(phase)
        if target_index != current_index + 1:
            raise StorageUpgradeError(
                f"migration phase must advance exactly once: {self.phase} -> {phase}",
                stage="journal",
                recoverable=True,
                diagnostic_path=self.path,
            )
        self.phase = phase
        self.updated_at = _now()
        if report is not None:
            self.reports[phase.value] = report
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "migration_id": self.migration_id,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reports": self.reports,
        }
        file_descriptor, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


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
