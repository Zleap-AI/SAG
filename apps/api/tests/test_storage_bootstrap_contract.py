from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.registry import select_adapter
from sag_api.upgrades.types import StorageProbe, StorageUpgradeError, StorageVersion


def test_registry_selects_071_adapter_without_exposing_legacy_details() -> None:
    probe = StorageProbe(StorageVersion.LEGACY_0_7, "fixture")

    adapter = select_adapter(probe, target_version="0.8.2")

    assert adapter is not None
    assert adapter.migration_id == "zleap-sag-0.7.1-to-0.8.2"


def test_active_engine_pointer_must_remain_below_storage_root(tmp_path: Path) -> None:
    store = ActiveEngineStore(tmp_path / ".storage-upgrades" / "active-engine.json")

    with pytest.raises(StorageUpgradeError, match="storage root"):
        store.activate(tmp_path / "engine", tmp_path.parent / "outside")


@pytest.mark.parametrize(
    "target",
    (
        pytest.param(lambda configured: configured, id="configured-engine"),
        pytest.param(lambda configured: configured / "nested", id="nested-engine"),
    ),
)
def test_active_engine_pointer_requires_a_sibling_target(tmp_path: Path, target) -> None:
    configured = tmp_path / "engine"
    store = ActiveEngineStore(tmp_path / ".storage-upgrades" / "active-engine.json")

    with pytest.raises(StorageUpgradeError, match="storage root"):
        store.activate(configured, target(configured))


def test_package_root_does_not_import_a_version_specific_adapter() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sag_api.upgrades; "
            "assert not any(name.startswith('sag_api.upgrades.zleap_sag_0_7_to_0_8') for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
