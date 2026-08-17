from __future__ import annotations

from sag_api.upgrades.contracts import StorageUpgradeContext, UpgradeReport
from sag_api.upgrades.types import StorageProbe, StorageVersion
from sag_api.upgrades.zleap_sag_0_7_to_0_8.migrator import migrate_071_to_082


class ZleapSag071To082Adapter:
    migration_id = "zleap-sag-0.7.1-to-0.8.2"
    source_version = "0.7.1"
    target_version = "0.8.2"

    def matches(self, probe: StorageProbe) -> bool:
        return probe.version is StorageVersion.LEGACY_0_7

    async def migrate(self, context: StorageUpgradeContext) -> UpgradeReport:
        return await migrate_071_to_082(context)
