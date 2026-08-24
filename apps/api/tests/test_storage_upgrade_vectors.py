from __future__ import annotations

import pytest

from sag_api.upgrades.types import StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.relational import (
    GenerationIdentity,
    RelationalMigrationReport,
)
from sag_api.upgrades.zleap_sag_0_7_to_0_8.vectors import _convert


def _relational_report() -> RelationalMigrationReport:
    return RelationalMigrationReport(
        generation_by_source={
            ("source-config", "source"): GenerationIdentity(
                generation_id="generation",
                source_version="source-version",
                chunk_version="chunk-version",
            )
        },
        source_by_event={},
        row_counts={},
    )


def test_missing_legacy_heading_vector_reuses_content_vector() -> None:
    content_vector = [0.1, 0.2, 0.3]

    record = _convert(
        "source_chunks",
        {
            "id": "chunk",
            "source_config_id": "source-config",
            "source_id": "source",
            "heading_vector": None,
            "content_vector": content_vector,
        },
        _relational_report(),
    )

    assert record.vectors["heading_vector"] == content_vector
    assert record.vectors["content_vector"] == content_vector


def test_missing_legacy_content_vector_still_blocks_migration() -> None:
    with pytest.raises(StorageUpgradeError, match="content_vector"):
        _convert(
            "source_chunks",
            {
                "id": "chunk",
                "source_config_id": "source-config",
                "source_id": "source",
                "heading_vector": [0.1, 0.2, 0.3],
                "content_vector": None,
            },
            _relational_report(),
        )
