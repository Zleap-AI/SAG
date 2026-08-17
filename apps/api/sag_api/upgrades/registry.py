from __future__ import annotations

from typing import Any

from sag_api.core.config import Settings
from sag_api.upgrades.contracts import StorageUpgradeAdapter, StorageUpgradeContext, UpgradeReport
from sag_api.upgrades.detector import detect_storage
from sag_api.upgrades.types import StorageLayout, StorageProbe, StorageUpgradeError, StorageVersion
from sag_api.upgrades.zleap_sag_0_7_to_0_8.adapter import ZleapSag071To082Adapter


def registered_adapters() -> tuple[StorageUpgradeAdapter, ...]:
    return (ZleapSag071To082Adapter(),)


def select_adapter(probe: StorageProbe, target_version: str) -> StorageUpgradeAdapter | None:
    for adapter in registered_adapters():
        if adapter.target_version == target_version and adapter.matches(probe):
            return adapter
    return None


async def ensure_storage_compatible(settings: Settings, session_factory: Any) -> UpgradeReport:
    """Dispatch a detected local storage layout through its registered adapter."""
    layout = StorageLayout.from_settings(settings)
    probe = detect_storage(layout, settings)
    adapter = select_adapter(probe, target_version="0.8.2")
    if adapter is not None:
        return await adapter.migrate(StorageUpgradeContext(settings=settings, session_factory=session_factory))
    if probe.version is StorageVersion.EMPTY:
        return UpgradeReport("empty")
    if probe.version is StorageVersion.CURRENT:
        return UpgradeReport("current")
    if probe.version is StorageVersion.UNSUPPORTED:
        return UpgradeReport("not_applicable")
    raise StorageUpgradeError(
        probe.reason,
        stage="detect",
        recoverable=True,
        diagnostic_path=layout.upgrades / "journal.json",
    )
