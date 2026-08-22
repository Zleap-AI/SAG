from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sag_api.upgrades.contracts import StorageBootstrapPhase, StorageChoice
from sag_api.upgrades.types import StorageUpgradeError

BOOTSTRAP_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BootstrapState:
    phase: StorageBootstrapPhase
    source_version: str | None
    target_version: str
    choice: StorageChoice | None = None
    actor_user_id: str | None = None
    adapter_id: str | None = None
    stage: str | None = None
    report: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    preserved_path: str | None = None
    diagnostic_path: str | None = None
    schema_version: int = BOOTSTRAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        now = _now()
        self.created_at = self.created_at or now
        self.updated_at = self.updated_at or now


class BootstrapStateStore:
    """Atomically persist the version-neutral bootstrap decision and status."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> BootstrapState | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != BOOTSTRAP_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            payload["phase"] = StorageBootstrapPhase(payload["phase"])
            if payload.get("choice") is not None:
                payload["choice"] = StorageChoice(payload["choice"])
            return BootstrapState(**payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise StorageUpgradeError(
                "storage bootstrap state is invalid",
                stage="bootstrap_state",
                recoverable=True,
                diagnostic_path=self.path,
            ) from error

    def save(self, state: BootstrapState) -> None:
        state.updated_at = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        payload["phase"] = state.phase.value
        payload["choice"] = state.choice.value if state.choice else None
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
