from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote

from sag_api.core.config import Settings


class StorageVersion(StrEnum):
    EMPTY = "empty"
    CURRENT = "current"
    LEGACY_0_7 = "legacy_0_7"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class MigrationPhase(StrEnum):
    DETECTED = "detected"
    BACKED_UP = "backed_up"
    RELATIONAL_MIGRATED = "relational_migrated"
    VECTORS_MIGRATED = "vectors_migrated"
    CHECKPOINTS_MIGRATED = "checkpoints_migrated"
    VERIFIED = "verified"
    SWAPPED = "swapped"
    COMPLETED = "completed"


MIGRATION_PHASES = tuple(MigrationPhase)


class StorageUpgradeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        recoverable: bool,
        backup_path: Path | None = None,
        diagnostic_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.recoverable = recoverable
        self.backup_path = backup_path
        self.diagnostic_path = diagnostic_path


def _sqlite_path(database_url: str) -> Path | None:
    normalized = database_url.replace("sqlite+aiosqlite", "sqlite", 1)
    prefix = "sqlite:///"
    if not normalized.startswith(prefix):
        return None
    raw_path = unquote(normalized[len(prefix) :].partition("?")[0])
    return Path(raw_path).expanduser().resolve()


@dataclass(frozen=True)
class StorageLayout:
    root: Path
    engine: Path
    sag_db: Path | None
    upgrades: Path
    backups: Path
    staging: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> StorageLayout:
        engine = Path(settings.data_dir).expanduser().resolve()
        root = engine.parent
        upgrades = root / ".storage-upgrades"
        return cls(
            root=root,
            engine=engine,
            sag_db=_sqlite_path(settings.database_url),
            upgrades=upgrades,
            backups=upgrades / "backups",
            staging=upgrades / "staging",
        )


@dataclass(frozen=True)
class StorageProbe:
    version: StorageVersion
    reason: str
    relational_columns: dict[str, set[str]] = field(default_factory=dict)
    vector_tables: set[str] = field(default_factory=set)
    schema_version: str | None = None
