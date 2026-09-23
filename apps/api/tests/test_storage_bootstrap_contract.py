from __future__ import annotations

from pathlib import Path

import pytest

from sag_api.upgrades.active_engine import ActiveEngineStore
from sag_api.upgrades.types import StorageUpgradeError


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
