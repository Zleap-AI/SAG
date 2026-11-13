from types import SimpleNamespace

import pytest

from sag_api.core.errors import ValidationError
from sag_api.services.knowledge_algorithms import require_sag1_octx


def test_legacy_sources_keep_octx_support():
    require_sag1_octx(SimpleNamespace())
    require_sag1_octx(SimpleNamespace(engine_kind="sag1"))


def test_sag2_rejects_octx_operations():
    with pytest.raises(ValidationError, match="SAG2"):
        require_sag1_octx(SimpleNamespace(engine_kind="sag2"))
