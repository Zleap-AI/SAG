"""Persisted model settings must not invent a MinerU provider over the environment."""

import pytest

from sag_api.core.config import Settings
from sag_api.parsing.mineru import MinerU302Client, MinerUClient
from sag_api.parsing.mineru_official import OfficialMinerUClient
from sag_api.services.settings_service import _normalize_overrides, apply_overrides


@pytest.mark.parametrize("persisted", [{}, {"search_top_k": 12}])
def test_unrelated_persisted_settings_preserve_official_mineru_environment(monkeypatch, persisted):
    monkeypatch.setenv("SAG_MINERU_PROVIDER", "official")
    monkeypatch.setenv("SAG_MINERU_BASE_URL", "https://mineru.net/api/v4")
    monkeypatch.setenv("SAG_MINERU_API_KEY", "test-mineru-key")
    configured = Settings(_env_file=None)

    # Startup normalizes the loaded row, then applies the resulting overrides.
    normalized = _normalize_overrides(persisted)
    apply_overrides(configured, normalized)

    assert "mineru_provider" not in normalized
    assert configured.mineru_provider == "official"
    assert configured.mineru_base_url == "https://mineru.net/api/v4"
    assert isinstance(MinerUClient(configured), OfficialMinerUClient)


@pytest.mark.parametrize(
    ("persisted", "provider", "client_type"),
    [
        ({"mineru_provider": "302", "mineru_base_url": "https://api.302ai.cn"}, "302", MinerU302Client),
        ({"mineru_base_url": "https://api.302ai.cn"}, "302", MinerU302Client),
        ({"mineru_base_url": "https://mineru.net/api/v4"}, "official", OfficialMinerUClient),
    ],
)
def test_explicit_and_legacy_mineru_settings_still_override_environment(
    monkeypatch, persisted, provider, client_type
):
    monkeypatch.setenv("SAG_MINERU_PROVIDER", "official")
    monkeypatch.setenv("SAG_MINERU_BASE_URL", "https://mineru.net/api/v4")
    monkeypatch.setenv("SAG_MINERU_API_KEY", "test-mineru-key")
    configured = Settings(_env_file=None)

    apply_overrides(configured, _normalize_overrides(persisted))

    assert configured.mineru_provider == provider
    assert configured.mineru_base_url == persisted["mineru_base_url"]
    assert isinstance(MinerUClient(configured), client_type)
