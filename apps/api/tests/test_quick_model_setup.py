"""302.AI 首次快捷配置：状态判定、完整预设、密钥脱敏与防覆盖。"""

import httpx
import pytest

from sag_api.core.config import settings

_RESTORE = (
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "llm_context_window",
    "llm_timeout_ms",
    "llm_max_retries",
    "document_chunk_max_tokens",
    "document_chunk_mode",
    "embedding_model",
    "embedding_base_url",
    "embedding_api_key",
    "embedding_dimensions",
    "document_parser",
    "mineru_base_url",
    "mineru_api_key",
    "mineru_version",
    "document_extract_concurrency",
    "search_strategy",
    "search_top_k",
    "sag_language",
)


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "quick-setup@t.com", "password": "password123"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_302_quick_model_setup(monkeypatch: pytest.MonkeyPatch):
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    snapshot = {key: getattr(settings, key) for key in _RESTORE}
    fake_key = "sk-test-quick-setup-123"
    transport = httpx.ASGITransport(app=app)

    try:
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(
                    delete(Setting).where(
                        Setting.scope == "global", Setting.key == "model_config"
                    )
                )
                await session.commit()

            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                headers = await _register(client)

                status = await client.get("/api/v1/system/model-setup", headers=headers)
                assert status.status_code == 200
                assert status.json() == {
                    "required": True,
                    "environment_configured": False,
                    "database_configured": False,
                }

                # 环境变量已有 Key 时不弹首次配置；恢复为空后才允许快捷配置。
                monkeypatch.setenv("SAG_LLM_API_KEY", "sk-test-env-only")
                env_status = await client.get("/api/v1/system/model-setup", headers=headers)
                assert env_status.json() == {
                    "required": False,
                    "environment_configured": True,
                    "database_configured": False,
                }
                assert "sk-test-env-only" not in env_status.text
                monkeypatch.setenv("SAG_LLM_API_KEY", "")

                invalid = await client.post(
                    "/api/v1/system/model-setup/302", headers=headers, json={"api_key": "   "}
                )
                assert invalid.status_code == 422

                response = await client.post(
                    "/api/v1/system/model-setup/302",
                    headers=headers,
                    json={"api_key": f"  {fake_key}  "},
                )
                assert response.status_code == 200, response.text
                assert fake_key not in response.text

                body = response.json()
                config = body["config"]
                assert config == {
                    "llm_provider": "openai",
                    "llm_base_url": "https://api.302ai.cn/v1",
                    "llm_model": "qwen3.6-flash",
                    "llm_temperature": 0.3,
                    "llm_max_tokens": 20_000,
                    "llm_context_window": 128_000,
                    "llm_timeout_ms": 60_000,
                    "llm_max_retries": 2,
                    "document_chunk_max_tokens": 1_000,
                    "document_chunk_mode": "standard",
                    "llm_api_key_set": True,
                    "embedding_model": "Qwen/Qwen3-Embedding-4B",
                    "embedding_base_url": "https://api.302ai.cn/v1",
                    "embedding_dimensions": 1024,
                    "embedding_api_key_set": True,
                    "document_parser": "auto",
                    "effective_document_parser": "mineru",
                    "mineru_base_url": "https://api.302ai.cn",
                    "mineru_version": "2.5",
                    "mineru_api_key_set": True,
                    "document_extract_concurrency": 5,
                    "search_strategy": "vector",
                    "search_top_k": 8,
                    "sag_language": "zh",
                }
                assert body["capabilities"]["llm_configured"] is True
                assert body["capabilities"]["llm_provider"] == "openai"
                assert body["capabilities"]["search_strategy"] == "vector"
                assert settings.llm_api_key == fake_key
                assert settings.embedding_api_key == fake_key
                assert settings.mineru_api_key == fake_key
                assert settings.effective_document_parser == "mineru"

                configured_status = await client.get(
                    "/api/v1/system/model-setup", headers=headers
                )
                assert configured_status.json() == {
                    "required": False,
                    "environment_configured": False,
                    "database_configured": True,
                }

                # 快捷入口只负责首次配置，不覆盖数据库里已经存在的设置。
                conflict = await client.post(
                    "/api/v1/system/model-setup/302",
                    headers=headers,
                    json={"api_key": "sk-test-replacement"},
                )
                assert conflict.status_code == 409
                assert "sk-test-replacement" not in conflict.text
                assert settings.llm_api_key == fake_key
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(Setting).where(Setting.scope == "global", Setting.key == "model_config")
            )
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)
