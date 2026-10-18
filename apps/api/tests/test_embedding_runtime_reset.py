"""Dimension saves must publish the same embedding policy to warm and cold engines."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1 import system
from sag_api.core.config import Settings
from sag_api.db.models import Document, Setting
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.engine_manager import EngineManager, _Slot
from sag_api.schemas.system import ModelConfigUpdate
from sag_api.services import settings_service


@pytest.mark.parametrize(
    ("initial", "patch", "expected_dimensions", "expected_reset"),
    [
        pytest.param({}, {"embedding_dimensions": 1024}, (1024, 1024), True, id="enable-request-dimensions"),
        pytest.param({}, {"search_top_k": 9}, (1024, None), False, id="unrelated-retrieval-save"),
        pytest.param({}, {"embedding_dimensions": None}, (1024, None), False, id="clear-native-dimensions"),
        pytest.param(
            {"embedding_base_url": "https://embedding.example/v1"},
            {"embedding_dimensions": 1024}, (1024, 1024), False, id="explicit-alias-same-effective-policy",
        ),
        pytest.param(
            {"embedding_base_url": "https://embedding.example/v1"},
            {"embedding_dimensions": None}, (1024, 1024), False, id="clear-alias-same-effective-policy",
        ),
        pytest.param(
            {"embedding_dimensions": 1024, "embedding_request_dimensions": 1024},
            {"embedding_dimensions": None}, (1024, None), True, id="restore-native-request-policy",
        ),
        pytest.param(
            {"embedding_request_dimensions": 768},
            {"embedding_dimensions": 1024}, (1024, 1024), True, id="replace-legacy-split-request-policy",
        ),
        pytest.param(
            {"embedding_request_dimensions": 768},
            {"mineru_version": "2.0"}, (1024, 768), False, id="parser-save-preserves-legacy-split",
        ),
        pytest.param(
            {"embedding_schema_dimensions": 16, "embedding_request_dimensions": 16},
            {"embedding_dimensions": 1024}, (1024, 1024), True, id="change-schema-and-request-dimensions",
        ),
        pytest.param(
            {"embedding_dimensions": 1024, "embedding_request_dimensions": 1024},
            {"embedding_dimensions": 1024}, (1024, 1024), False, id="repeat-explicit-policy",
        ),
    ],
)
async def test_dimension_save_resets_only_when_effective_engine_policy_changes(
    monkeypatch, initial, patch, expected_dimensions, expected_reset,
):
    configured = Settings(
        _env_file=None,
        **{
            "embedding_model": "BAAI/bge-m3",
            "embedding_base_url": "https://api.siliconflow.cn/v1",
            "embedding_dimensions": None,
            "embedding_schema_dimensions": 1024,
            "embedding_request_dimensions": None,
            **initial,
        },
    )
    manager = EngineManager(configured.model_copy(deep=True))
    cached_config = build_engine_config(manager._settings)
    cached_engine = SimpleNamespace(aclose=AsyncMock())
    manager._slots["warm-source"] = _Slot(engine=cached_engine, config=cached_config)
    applied = []

    class Runtime:
        ready = True

        async def apply_settings(self, values, *, reset_engines):
            applied.append(reset_engines)
            await manager.apply_settings(values.model_copy(deep=True), reset_engines=reset_engines)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(knowledge_runtime=Runtime())))
    monkeypatch.setattr(settings_service, "_settings", configured)
    monkeypatch.setattr(settings_service, "_MODEL_CONFIG_SAVE_LOCK", asyncio.Lock())
    monkeypatch.setattr(settings_service, "_MODEL_CONFIG_SOURCES", dict(settings_service._MODEL_CONFIG_SOURCES))
    monkeypatch.setattr(system, "settings", configured)
    monkeypatch.setattr(system, "_capabilities", lambda **_kwargs: {})

    database = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with database.begin() as connection:
            await connection.run_sync(lambda connection: Setting.__table__.create(connection))
            await connection.run_sync(lambda connection: Document.__table__.create(connection))
        async with async_sessionmaker(database, expire_on_commit=False)() as session:
            await system.update_model_config(ModelConfigUpdate.model_validate(patch), request, object(), session)

        assert applied == [expected_reset]
        assert cached_engine.aclose.await_count == int(expected_reset)
        assert bool(manager._slots) is not expected_reset
        cold_config = build_engine_config(manager._settings)
        cold_dimensions = (cold_config.embedding.schema_dimensions, cold_config.embedding.request_dimensions)
        assert cold_dimensions == expected_dimensions
        if not expected_reset:
            assert (cached_config.embedding.schema_dimensions, cached_config.embedding.request_dimensions) == (
                cold_dimensions
            )
    finally:
        await database.dispose()
