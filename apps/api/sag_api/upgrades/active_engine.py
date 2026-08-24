from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sag_api.upgrades.directory_replace import replace_path
from sag_api.upgrades.types import StorageUpgradeError


class ActiveEngineStore:
    """Persist the active engine only when it remains inside the storage root."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve(self, configured: Path) -> Path:
        configured_path = configured.expanduser().resolve()
        if not self.path.is_file():
            return configured_path

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("unsupported schema version")
            stored_configured = Path(payload["configured_engine"]).expanduser().resolve()
            active = Path(payload["active_engine"]).expanduser().resolve()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageUpgradeError(
                "active engine pointer is invalid",
                stage="active_engine",
                recoverable=True,
                diagnostic_path=self.path,
            ) from exc

        if stored_configured != configured_path:
            raise StorageUpgradeError(
                "active engine pointer does not match the configured engine",
                stage="active_engine",
                recoverable=True,
                diagnostic_path=self.path,
            )
        self._require_below_storage_root(configured_path, active)
        return active

    def activate(self, configured: Path, target: Path) -> None:
        configured_path = configured.expanduser().resolve()
        target_path = target.expanduser().resolve()
        self._require_below_storage_root(configured_path, target_path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = {
            "schema_version": 1,
            "configured_engine": str(configured_path),
            "active_engine": str(target_path),
        }
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            replace_path(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_below_storage_root(self, configured: Path, target: Path) -> None:
        storage_root = configured.parent
        if target.parent != storage_root or target == configured:
            raise StorageUpgradeError(
                "active engine target must remain below the storage root",
                stage="active_engine",
                recoverable=False,
                diagnostic_path=self.path,
            )
