from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from sag_api.core.config import Settings
from sag_api.upgrades.types import StorageProbe


class StorageChoice(StrEnum):
    MIGRATE = "migrate"
    FRESH = "fresh"


class StorageBootstrapPhase(StrEnum):
    READY = "ready"
    CHOICE_REQUIRED = "choice_required"
    PROCESSING = "processing"
    FAILED = "failed"


class FreshWorkspacePhase(StrEnum):
    TARGET_CREATED = "target_created"
    BUSINESS_BACKED_UP = "business_backed_up"
    BUSINESS_CLEARED = "business_cleared"
    POINTER_ACTIVATED = "pointer_activated"
    COMPLETED = "completed"


@dataclass(frozen=True)
class StorageBootstrapStatus:
    phase: StorageBootstrapPhase
    detected_version: str | None
    target_version: str
    choices: tuple[StorageChoice, ...] = ()
    stage: str | None = None
    error: str | None = None
    recoverable: bool = False
    runtime_ready: bool = False
    preserved_path: Path | None = None


@dataclass(frozen=True)
class StorageUpgradeContext:
    settings: Settings
    session_factory: Any


@dataclass(frozen=True)
class UpgradeReport:
    status: str
    report_path: Path | None = None
    backup_path: Path | None = None


class StorageUpgradeAdapter(Protocol):
    migration_id: str
    source_version: str
    target_version: str

    def matches(self, probe: StorageProbe) -> bool: ...

    async def migrate(self, context: StorageUpgradeContext) -> UpgradeReport: ...
