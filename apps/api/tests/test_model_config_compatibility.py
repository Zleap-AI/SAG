"""A JEV opt-in must not reinterpret an existing workspace's saved model profile."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import Setting
from sag_api.services import settings_service


@pytest.fixture
async def legacy_profile(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'application.db'}")
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(database, expire_on_commit=False)
    configured = Settings(_env_file=None, llm_model="environment-model", embedding_model="environment-embedding")
    monkeypatch.setattr(settings_service, "_settings", configured)
    monkeypatch.setattr(settings_service, "_MODEL_CONFIG_SOURCES", dict(settings_service._MODEL_CONFIG_SOURCES))
    try:
        yield sessions, configured
    finally:
        await database.dispose()


async def save_legacy_row(sessions, values):
    async with sessions() as session:
        session.add(Setting(scope="global", key="model_config", value=values))
        await session.commit()


async def read_legacy_row(sessions):
    async with sessions() as session:
        row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
        return row.value


def test_saved_old_fields_keep_the_original_overlay_semantics():
    configured = Settings(_env_file=None)
    settings_service.apply_overrides(configured, {"llm_model": "old-model", "llm_temperature": None})
    assert configured.llm_model == "old-model"
    # Old versions accepted null here. JEV must not turn this into a new default.
    assert configured.llm_temperature is None


def test_invalid_unused_jev_does_not_block_existing_generation_settings():
    configured = Settings(_env_file=None, search_strategy="vector")
    settings_service.apply_overrides(configured, {"llm_model": "old-model", "jev_endpoint": "file:///invalid"})
    assert configured.llm_model == "old-model"
    assert configured.search_strategy == "vector"
    assert configured.jev_endpoint is None


@pytest.mark.parametrize("temperature", [None, 0.4])
async def test_upgrade_does_not_rewrite_old_saved_nulls_or_add_jev_fields(legacy_profile, temperature):
    sessions, configured = legacy_profile
    old = {"llm_model": "saved-old-model", "llm_temperature": temperature, "embedding_model": "saved-old-embedding"}
    await save_legacy_row(sessions, old)
    await settings_service.apply_startup_overrides(sessions)
    assert configured.llm_model == old["llm_model"]
    assert configured.embedding_model == old["embedding_model"]
    assert configured.llm_temperature == temperature
    assert await read_legacy_row(sessions) == old
    assert "recovery" not in settings_service.effective_model_config()


@pytest.mark.parametrize("temperature", [None, 0.4])
async def test_saving_another_card_preserves_old_values_and_does_not_configure_jev(legacy_profile, temperature):
    sessions, configured = legacy_profile
    old = {"llm_model": "saved-old-model", "llm_temperature": temperature, "embedding_model": "saved-old-embedding"}
    await save_legacy_row(sessions, old)
    await settings_service.apply_startup_overrides(sessions)
    async with sessions() as session:
        await settings_service.save_model_config(session, {"document_chunk_max_tokens": 1200})
    assert await read_legacy_row(sessions) == {**old, "document_chunk_max_tokens": 1200}
    assert configured.llm_model == old["llm_model"]
    assert configured.embedding_model == old["embedding_model"]
    assert configured.llm_temperature == temperature


@pytest.mark.parametrize("temperature", [None, 0.4])
async def test_jev_only_save_preserves_saved_legacy_model_identity(legacy_profile, temperature):
    sessions, configured = legacy_profile
    old = {"llm_model": "saved-old-model", "llm_temperature": temperature, "embedding_model": "saved-old-embedding"}
    await save_legacy_row(sessions, old)
    await settings_service.apply_startup_overrides(sessions)
    async with sessions() as session:
        await settings_service.save_model_config(session, {"jev_endpoint": "https://jev.example/score"})
    assert configured.llm_model == old["llm_model"]
    assert configured.embedding_model == old["embedding_model"]
    assert configured.llm_temperature == temperature
    stored = await read_legacy_row(sessions)
    assert stored == {**old, "jev_endpoint": "https://jev.example/score", "jev_api_key": None}
    assert configured.search_strategy != "multi_jev"


def test_legacy_environment_key_without_explicit_model_keeps_runtime_default(monkeypatch):
    monkeypatch.delenv("SAG_LLM_MODEL", raising=False)
    monkeypatch.setenv("SAG_LLM_API_KEY", "existing-environment-secret")
    configured = Settings(_env_file=None)
    assert configured.llm_model == "qwen3.6-flash"


@pytest.mark.parametrize("saved_model", ["old-custom-generation", "qwen3.8-flash"])
def test_explicit_saved_model_always_beats_runtime_default(saved_model):
    configured = Settings(_env_file=None)
    settings_service.apply_overrides(configured, {"llm_model": saved_model})
    assert configured.llm_model == saved_model


async def test_unused_bad_jev_draft_does_not_block_another_cards_save(legacy_profile):
    sessions, configured = legacy_profile
    old = {"llm_model": "saved-old-model", "jev_endpoint": "file:///invalid", "search_strategy": "vector"}
    await save_legacy_row(sessions, old)
    await settings_service.apply_startup_overrides(sessions)
    async with sessions() as session:
        await settings_service.save_model_config(session, {"document_chunk_max_tokens": 1200})
    assert await read_legacy_row(sessions) == {**old, "document_chunk_max_tokens": 1200}
    assert configured.llm_model == old["llm_model"]
    assert configured.search_strategy == "vector"
    assert configured.jev_endpoint is None


async def test_explicit_jev_key_cannot_be_bound_to_an_invalid_saved_endpoint(legacy_profile):
    from sag_api.core.errors import ConfigurationError

    sessions, configured = legacy_profile
    old = {"llm_model": "saved-old-model", "jev_endpoint": "file:///invalid"}
    await save_legacy_row(sessions, old)
    await settings_service.apply_startup_overrides(sessions)
    async with sessions() as session:
        with pytest.raises(ConfigurationError):
            await settings_service.save_model_config(session, {"jev_api_key": "new-scoring-secret"})
    assert await read_legacy_row(sessions) == old
    assert configured.jev_api_key is None
