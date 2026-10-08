"""首次快捷配置：预设、独立凭据、密钥脱敏与防覆盖。"""

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
    "mineru_provider",
    "mineru_base_url",
    "mineru_api_key",
    "mineru_version",
    "mineru_official_model",
    "document_extract_concurrency",
    "document_extraction_profile",
    "search_strategy",
    "search_top_k",
    "sag_language",
)


async def _register(client: httpx.AsyncClient, email: str = "quick-setup@t.com") -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123"},
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
                expected_config = {
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
                    "mineru_provider": "302",
                    "mineru_base_url": "https://api.302ai.cn",
                    "mineru_version": "2.5",
                    "mineru_official_model": "vlm",
                    "mineru_api_key_set": True,
                    "document_extract_concurrency": 30,
                    "search_strategy": "multi_es_fast",
                    "search_top_k": 8,
                    "sag_language": "zh",
                }
                assert config.items() >= expected_config.items()
                assert config["sources"]["llm_model"] == "database"
                assert config["locked_fields"] == []
                assert body["capabilities"]["llm_configured"] is True
                assert body["capabilities"]["llm_provider"] == "openai"
                assert body["capabilities"]["search_strategy"] == "multi_es_fast"
                assert settings.llm_api_key == fake_key
                assert settings.embedding_api_key == fake_key
                assert settings.mineru_api_key == fake_key
                assert settings.effective_document_parser == "mineru"
                runtime_settings = app.state.knowledge_runtime._settings
                assert app.state.llm._settings is runtime_settings
                assert app.state.engine_manager._settings is runtime_settings
                assert runtime_settings.llm_api_key == fake_key
                assert runtime_settings.embedding_api_key == fake_key
                assert runtime_settings.mineru_api_key == fake_key
                assert runtime_settings.embedding_model == "Qwen/Qwen3-Embedding-4B"

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


@pytest.mark.asyncio
async def test_deepseek_quick_setup_keeps_credentials_separate_and_preserves_parser(monkeypatch):
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app
    from sag_api.services.settings_service import apply_startup_overrides

    snapshot = {key: getattr(settings, key) for key in _RESTORE}
    deepseek_key = "sk-test-deepseek-key"
    embedding_key = "sk-test-302-embedding-key"
    monkeypatch.setattr(settings, "document_parser", "markitdown")
    monkeypatch.setattr(settings, "mineru_api_key", None)
    monkeypatch.setattr(settings, "search_strategy", "multi")
    try:
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
                await session.commit()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                headers = await _register(client, "deepseek-setup@t.com")
                payload = {"api_key": f" {deepseek_key} ", "embedding_api_key": f" {embedding_key} "}
                response = await client.post("/api/v1/system/model-setup/deepseek", headers=headers, json=payload)
                assert response.status_code == 200, response.text
                assert deepseek_key not in response.text
                assert embedding_key not in response.text
                config = response.json()["config"]
                assert config["llm_model"] == "deepseek-flash"
                assert config["llm_base_url"] == "https://api.deepseek.com"
                assert config["llm_provider"] == "openai"
                assert config["embedding_model"] == "Qwen/Qwen3-Embedding-4B"
                assert config["embedding_base_url"] == "https://api.302ai.cn/v1"
                assert config["embedding_dimensions"] == 1024
                assert config["document_parser"] == "markitdown"
                assert config["mineru_api_key_set"] is False
                assert config["search_strategy"] == "multi"
                runtime_settings = app.state.knowledge_runtime._settings
                assert runtime_settings.llm_api_key == deepseek_key
                assert runtime_settings.embedding_api_key == embedding_key
                assert runtime_settings.mineru_api_key is None
                async with SessionLocal() as session:
                    row = await session.scalar(
                        select(Setting).where(Setting.scope == "global", Setting.key == "model_config")
                    )
                    assert row.value["llm_api_key"] == deepseek_key
                    assert row.value["embedding_api_key"] == embedding_key
                    assert "mineru_api_key" not in row.value
                # The saved preset survives startup without reverting to the 302 generation endpoint.
                settings.llm_api_key = None
                settings.embedding_api_key = None
                await apply_startup_overrides(SessionLocal)
                assert settings.llm_model == "deepseek-flash"
                assert settings.llm_api_key == deepseek_key
                assert settings.embedding_api_key == embedding_key
                status = await client.get("/api/v1/system/model-setup", headers=headers)
                assert status.json()["required"] is False
                for endpoint, replacement in [
                    ("deepseek", {"api_key": "replacement", "embedding_api_key": "replacement"}),
                    ("302", {"api_key": "replacement"}),
                ]:
                    conflict = await client.post(
                        f"/api/v1/system/model-setup/{endpoint}", headers=headers, json=replacement
                    )
                    assert conflict.status_code == 409
                assert settings.llm_api_key == deepseek_key
                assert settings.embedding_api_key == embedding_key
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_deepseek_quick_setup_requires_both_keys_and_respects_deployment_config(monkeypatch):
    from sag_api.main import app

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            valid = {"api_key": "sk-test-deepseek", "embedding_api_key": "sk-test-302"}
            unauthorized = await client.post("/api/v1/system/model-setup/deepseek", json=valid)
            assert unauthorized.status_code == 401
            headers = await _register(client, "deepseek-validation@t.com")
            for payload in [
                {}, {"api_key": "sk-test-deepseek"},
                {**valid, "api_key": "   "}, {**valid, "embedding_api_key": "   "},
            ]:
                response = await client.post("/api/v1/system/model-setup/deepseek", headers=headers, json=payload)
                assert response.status_code == 422
            monkeypatch.setenv("SAG_LLM_API_KEY", "sk-test-environment")
            response = await client.post("/api/v1/system/model-setup/deepseek", headers=headers, json=valid)
            assert response.status_code == 409
            monkeypatch.setenv("SAG_LLM_API_KEY", "")
            monkeypatch.setattr(settings, "lock_llm_config", True)
            response = await client.post("/api/v1/system/model-setup/deepseek", headers=headers, json=valid)
            assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["302", "deepseek"])
@pytest.mark.parametrize(
    ("embedding", "model", "base_url"),
    [
        ("302", "Qwen/Qwen3-Embedding-4B", "https://api.302ai.cn/v1"),
        ("zhipu", "embedding-3", "https://open.bigmodel.cn/api/paas/v4"),
        ("bailian", "text-embedding-v4", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ],
)
async def test_quick_setup_persists_selected_embedding_and_its_own_key(generation, embedding, model, base_url):
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app
    from sag_api.services.settings_service import apply_startup_overrides

    snapshot = {key: getattr(settings, key) for key in _RESTORE}
    generation_key = f"sk-test-generation-{generation}"
    embedding_key = f"sk-test-embedding-{embedding}"
    try:
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
                await session.commit()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                headers = await _register(client, f"mix-{generation}-{embedding}@t.com")
                response = await client.post(
                    f"/api/v1/system/model-setup/{generation}",
                    headers=headers,
                    json={
                        "api_key": generation_key,
                        "embedding_provider": embedding,
                        "embedding_api_key": f" {embedding_key} ",
                    },
                )
                assert response.status_code == 200, response.text
                assert generation_key not in response.text
                assert embedding_key not in response.text
                config = response.json()["config"]
                assert config["embedding_model"] == model
                assert config["embedding_base_url"] == base_url
                assert config["embedding_dimensions"] == 1024
                runtime_settings = app.state.knowledge_runtime._settings
                assert runtime_settings.embedding_api_key == embedding_key
                assert runtime_settings.llm_api_key == generation_key
                assert runtime_settings.embedding_model == model
                assert runtime_settings.effective_embedding_request_dimensions == 1024
                if generation == "302":
                    assert runtime_settings.mineru_api_key == generation_key
                async with SessionLocal() as session:
                    row = await session.scalar(
                        select(Setting).where(Setting.scope == "global", Setting.key == "model_config")
                    )
                    assert row.value["embedding_api_key"] == embedding_key
                    assert row.value["embedding_base_url"] == base_url
                settings.embedding_api_key = None
                await apply_startup_overrides(SessionLocal)
                assert settings.embedding_api_key == embedding_key
                assert settings.embedding_model == model
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_quick_setup_rejects_missing_or_unknown_embedding_credentials_before_writing():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            headers = await _register(client, "embedding-validation@t.com")
            for generation in ("302", "deepseek"):
                for embedding in ("zhipu", "bailian"):
                    for missing_key in ({}, {"embedding_api_key": "   "}, {"embedding_api_key": None}):
                        response = await client.post(
                            f"/api/v1/system/model-setup/{generation}", headers=headers,
                            json={"api_key": "sk-test-generation", "embedding_provider": embedding, **missing_key},
                        )
                        assert response.status_code == 422, response.text
                response = await client.post(
                    f"/api/v1/system/model-setup/{generation}", headers=headers,
                    json={"api_key": "sk-test-generation", "embedding_provider": "unknown", "embedding_api_key": "key"},
                )
                assert response.status_code == 422, response.text
        async with SessionLocal() as session:
            row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            assert row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["302", "deepseek"])
@pytest.mark.parametrize("embedding", ["zhipu", "bailian"])
async def test_quick_setup_cannot_replace_embedding_space_of_existing_documents(generation, embedding):
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Document, Setting, Source
    from sag_api.enums import DocumentStatus
    from sag_api.main import app

    snapshot = {key: getattr(settings, key) for key in _RESTORE}
    source_id = None
    try:
        async with app.router.lifespan_context(app):
            async with SessionLocal() as session:
                source = Source(name="quick-setup-index-guard", sag_source_config_id="quick-setup-index-guard")
                session.add(source)
                await session.flush()
                source_id = source.id
                session.add(Document(
                    source_id=source.id, filename="indexed.md", content_type="text/markdown",
                    storage_path="/tmp/indexed.md", status=DocumentStatus.READY, sag_source_id="indexed-document",
                ))
                await session.commit()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                headers = await _register(client, f"guard-{generation}-{embedding}@t.com")
                response = await client.post(
                    f"/api/v1/system/model-setup/{generation}", headers=headers,
                    json={"api_key": "sk-test-generation", "embedding_provider": embedding,
                          "embedding_api_key": "sk-test-embedding"},
                )
                assert response.status_code == 409, response.text
                assert "已有入库文档" in response.text
                assert settings.embedding_model == snapshot["embedding_model"]
                assert settings.llm_api_key == snapshot["llm_api_key"]
                async with SessionLocal() as session:
                    row = await session.scalar(
                        select(Setting).where(Setting.scope == "global", Setting.key == "model_config")
                    )
                    assert row is None
                    document = await session.scalar(select(Document).where(Document.source_id == source_id))
                    assert document.status == DocumentStatus.READY
    finally:
        async with SessionLocal() as session:
            if source_id:
                await session.execute(delete(Document).where(Document.source_id == source_id))
                await session.execute(delete(Source).where(Source.id == source_id))
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)
