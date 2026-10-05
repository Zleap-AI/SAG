"""Native read storage reuse and mutation separation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.core.config import settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.engine_access import EngineAccess


@pytest.fixture
def access():
    config = build_engine_config(settings)
    factory = object()
    slot = SimpleNamespace(
        closing=False, last_used=0,
        engine=SimpleNamespace(
            _config=config,
            resources=SimpleNamespace(relational=SimpleNamespace(session_factory=lambda: factory)),
        ),
    )
    manager = SimpleNamespace(
        _slots={"extracting": slot},
        _config_for=lambda source: config if source is None else source,
        _slot=AsyncMock(return_value="stock-slot"),
        _relational_session_factory=AsyncMock(return_value="stock-factory"),
    )
    return EngineAccess(manager), slot, factory


@pytest.mark.asyncio
async def test_cold_read_reuses_compatible_storage_without_creating_or_aliasing_engine(access):
    reader, shared, factory = access
    assert await reader.slot("cold") is shared
    assert await reader.relational_session_factory("cold") is factory
    assert shared.last_used > 0
    assert list(reader._manager._slots) == ["extracting"]
    reader._manager._slot.assert_not_awaited()
    reader._manager._relational_session_factory.assert_not_awaited()
    # Mutations still enter the original manager, rather than the read-side seam.
    assert await reader._manager._slot("cold") == "stock-slot"


@pytest.mark.parametrize("state", ["empty", "closing", "warm"])
@pytest.mark.asyncio
async def test_stock_path_preserved_without_a_reusable_slot(access, state):
    reader, shared, _ = access
    if state == "empty":
        reader._manager._slots.clear()
    elif state == "closing":
        shared.closing = True
    else:
        reader._manager._slots["cold"] = shared
    assert await reader.slot("cold") == "stock-slot"
    assert await reader.relational_session_factory("cold") == "stock-factory"
    reader._manager._slot.assert_awaited_once_with("cold", None)
    reader._manager._relational_session_factory.assert_awaited_once_with("cold", None)


@pytest.mark.parametrize("kind", ["relational", "vector", "embedding"])
@pytest.mark.asyncio
async def test_different_storage_or_embedding_identity_never_reused(access, kind):
    reader, shared, _ = access
    wanted = shared.engine._config.model_copy(deep=True)
    if kind == "embedding":
        wanted.embedding.model = "different-weights"
    else:
        setattr(wanted, kind, getattr(wanted, kind).model_copy(update={"path": "/different-store"}))
    assert await reader.slot("cold", wanted) == "stock-slot"
    assert await reader.relational_session_factory("cold", wanted) == "stock-factory"
    reader._manager._slot.assert_awaited_once_with("cold", wanted)


@pytest.mark.asyncio
async def test_provider_failure_propagates_without_selecting_another_backend(access):
    reader, shared, _ = access

    def fail():
        raise RuntimeError("temporary database failure")

    shared.engine.resources.relational.session_factory = fail
    with pytest.raises(RuntimeError, match="temporary database failure"):
        await reader.relational_session_factory("cold")
    reader._manager._relational_session_factory.assert_not_awaited()
