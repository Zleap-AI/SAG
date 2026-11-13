"""Knowledge algorithms are chosen once, independently of retrieval preferences."""

import pytest
from pydantic import ValidationError

from sag_api.core.db import _COLUMN_UPGRADES
from sag_api.db.models import Source
from sag_api.schemas.source import SourceCreate, SourceUpdate


def test_source_creation_defaults_to_sag1():
    assert SourceCreate(name="legacy compatible").engine_kind == "sag1"


@pytest.mark.parametrize("kind", ["sag1", "sag2"])
def test_source_creation_accepts_two_algorithms(kind):
    assert SourceCreate(name="test", engine_kind=kind).engine_kind == kind


@pytest.mark.parametrize("kind", ["vector", "multi", "multi_jev", "invalid"])
def test_source_creation_rejects_retrieval_strategy_as_algorithm(kind):
    with pytest.raises(ValidationError):
        SourceCreate(name="test", engine_kind=kind)


def test_edit_cannot_change_algorithm():
    with pytest.raises(ValidationError):
        SourceUpdate(engine_kind="sag1")


def test_legacy_source_migration_does_not_infer_sag2_from_preference():
    assert _COLUMN_UPGRADES["sources"]["engine_kind"] == "VARCHAR(16) NOT NULL DEFAULT 'sag1'"
    assert str(Source.__table__.c.engine_kind.server_default.arg) == "sag1"
