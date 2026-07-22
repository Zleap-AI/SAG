"""模型配置端点：GET 脱敏、PUT 持久化+生效、密钥保留、非法值 422、连接测试（离线）。

全程离线且**不留全局副作用**：`finally` 删除 settings 表行 + 还原被改的 `settings` 单例字段，
避免跨测试泄漏（端点会就地覆盖进程级单例）。连接测试只验证「未配置」分支（无网络）。
"""

import httpx
import pytest

from sag_api.core.config import Settings, settings

_RESTORE = (
    "llm_provider",
    "llm_base_url",
    "llm_model",
    "llm_temperature",
    "llm_timeout_ms",
    "llm_max_retries",
    "search_strategy",
    "document_chunk_max_tokens",
    "document_chunk_mode",
    "search_top_k",
    "sag_language",
    "llm_api_key",
    "document_parser",
    "mineru_base_url",
    "mineru_api_key",
    "mineru_version",
    "timezone",
    "document_extract_concurrency",
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000", ["http://localhost:3000"]),
        (
            "http://localhost:3000,https://sag.example.com",
            ["http://localhost:3000", "https://sag.example.com"],
        ),
        ('["http://localhost:3000"]', ["http://localhost:3000"]),
    ],
)
def test_cors_origins_env_formats(monkeypatch, raw, expected):
    monkeypatch.setenv("SAG_CORS_ORIGINS", raw)
    assert Settings(_env_file=None).cors_origins == expected


def test_legacy_atomic_env_strategy_maps_to_precise(monkeypatch):
    monkeypatch.setenv("SAG_SEARCH_STRATEGY", "atomic")
    assert Settings(_env_file=None).search_strategy == "multi"


def test_timezone_defaults_to_beijing_and_rejects_invalid(monkeypatch):
    monkeypatch.delenv("SAG_TIMEZONE", raising=False)
    assert Settings(_env_file=None).timezone == "Asia/Shanghai"
    monkeypatch.setenv("SAG_TIMEZONE", "UTC")
    assert Settings(_env_file=None).timezone == "UTC"
    monkeypatch.setenv("SAG_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_provider_base_urls_default_to_302_china_endpoint(monkeypatch):
    for name in ("SAG_LLM_BASE_URL", "SAG_EMBEDDING_BASE_URL", "SAG_MINERU_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    configured = Settings(_env_file=None)
    assert configured.llm_provider == "openai"
    assert configured.llm_base_url == "https://api.302ai.cn/v1"
    assert configured.embedding_base_url == "https://api.302ai.cn/v1"
    assert configured.mineru_base_url == "https://api.302ai.cn"


def test_default_model_output_limit_is_20000(monkeypatch):
    monkeypatch.delenv("SAG_LLM_MAX_TOKENS", raising=False)
    assert Settings(_env_file=None).llm_max_tokens == 20_000


@pytest.mark.asyncio
async def test_legacy_atomic_db_strategy_is_migrated():
    from sqlalchemy import delete, select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Setting
    from sag_api.services.settings_service import apply_startup_overrides

    await init_db()
    previous = {
        field: getattr(settings, field)
        for field in (
            "search_strategy",
            "llm_base_url",
            "embedding_base_url",
            "mineru_base_url",
        )
    }
    try:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            session.add(
                Setting(
                    scope="global",
                    key="model_config",
                    value={
                        "search_strategy": "atomic",
                        "llm_base_url": "https://api.302.ai/v1",
                        "embedding_base_url": "https://api.302.ai/v1",
                        "mineru_base_url": "https://api.302.ai",
                    },
                )
            )
            await session.commit()

        await apply_startup_overrides(SessionLocal)
        assert settings.search_strategy == "multi"
        assert settings.llm_base_url == "https://api.302ai.cn/v1"
        assert settings.embedding_base_url == "https://api.302ai.cn/v1"
        assert settings.mineru_base_url == "https://api.302ai.cn"

        async with SessionLocal() as session:
            row = await session.scalar(select(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            assert row is not None
            assert row.value["search_strategy"] == "multi"
            assert row.value["llm_base_url"] == "https://api.302ai.cn/v1"
            assert row.value["embedding_base_url"] == "https://api.302ai.cn/v1"
            assert row.value["mineru_base_url"] == "https://api.302ai.cn"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Setting).where(Setting.scope == "global", Setting.key == "model_config"))
            await session.commit()
        for field, value in previous.items():
            setattr(settings, field, value)


async def _register(c, email):
    r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    assert r.json()["user"]["created_at"].endswith(("Z", "+00:00"))
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_model_config_crud_masking_and_test(monkeypatch: pytest.MonkeyPatch):
    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Setting
    from sag_api.main import app

    snapshot = {k: getattr(settings, k) for k in _RESTORE}
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                A = await _register(c, "modelcfg@t.com")

                preferences = (await c.get("/api/v1/system/preferences", headers=A)).json()
                assert preferences["timezone"] == snapshot["timezone"]
                changed = await c.put(
                    "/api/v1/system/preferences",
                    headers=A,
                    json={"timezone": "UTC"},
                )
                assert changed.status_code == 200
                assert changed.json()["timezone"] == "UTC"
                assert settings.timezone == "UTC"
                assert (await c.get("/api/v1/system/capabilities")).json()["timezone"] == "UTC"
                invalid_timezone = await c.put(
                    "/api/v1/system/preferences",
                    headers=A,
                    json={"timezone": "Mars/Olympus"},
                )
                assert invalid_timezone.status_code == 422

                # GET：密钥脱敏为 *_set，离线下未配置
                body = (await c.get("/api/v1/system/model-config", headers=A)).json()
                assert "llm_api_key" not in body and body["llm_api_key_set"] is False
                assert body["llm_provider"] == "openai"
                assert "mineru_api_key" not in body and body["mineru_api_key_set"] is False
                assert body["effective_document_parser"] == "markitdown"
                assert body["document_extract_concurrency"] == 5
                assert body["document_chunk_max_tokens"] == 1_000
                assert body["document_chunk_mode"] == "standard"
                assert body["llm_timeout_ms"] == 60_000
                assert body["llm_max_retries"] == 2
                assert "search_top_k" in body and "sag_language" in body

                providers = (await c.get("/api/v1/system/model-providers", headers=A)).json()
                assert [provider["id"] for provider in providers] == [
                    "openai",
                    "anthropic",
                    "gemini",
                ]
                assert providers[0]["default_model"] == body["llm_model"]
                assert "litellm_prefix" not in providers[0]

                # 连接测试（未配置）→ 立即 ok False，无网络
                t = (await c.post("/api/v1/system/model-config/test", headers=A)).json()
                assert t["ok"] is False and "message" in t

                # 测试表单草稿：应使用未保存的 provider / key，但不写入全局配置。
                from sag_api.generation.llm import LLMClient

                observed: dict = {}

                async def fake_complete(client, _messages):
                    observed["provider"] = client._settings.llm_provider
                    observed["model"] = client._settings.llm_model
                    observed["key"] = client._settings.llm_api_key
                    return "pong"

                monkeypatch.setattr(LLMClient, "complete", fake_complete)
                draft = await c.post(
                    "/api/v1/system/model-config/test",
                    headers=A,
                    json={
                        "llm_provider": "gemini",
                        "llm_base_url": None,
                        "llm_api_key": "draft-secret",
                        "llm_model": "gemini-3.5-flash",
                    },
                )
                assert draft.status_code == 200
                assert draft.json() == {
                    "ok": True,
                    "message": "连接成功 · gemini / gemini-3.5-flash",
                }
                assert observed == {
                    "provider": "gemini",
                    "model": "gemini-3.5-flash",
                    "key": "draft-secret",
                }
                assert "draft-secret" not in draft.text
                assert settings.llm_provider == snapshot["llm_provider"]
                assert settings.llm_api_key == snapshot["llm_api_key"]

                # PUT 非密钥字段 → 持久化 + 生效 + capabilities 反映
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "llm_provider": "anthropic",
                        "llm_model": "test-model-x",
                        "llm_timeout_ms": 45_000,
                        "llm_max_retries": 3,
                        "document_chunk_max_tokens": 1_600,
                        "document_chunk_mode": "heading_strict",
                        "search_top_k": 5,
                        "sag_language": "en",
                    },
                )
                assert r.status_code == 200, r.text
                assert r.json()["config"]["llm_model"] == "test-model-x"
                assert r.json()["config"]["llm_provider"] == "anthropic"
                assert r.json()["capabilities"]["llm_provider"] == "anthropic"
                assert r.json()["capabilities"]["llm_model"] == "test-model-x"
                assert settings.llm_model == "test-model-x"  # 单例即时生效
                assert settings.llm_provider == "anthropic"
                assert settings.llm_timeout_ms == 45_000
                assert settings.llm_max_retries == 3
                assert settings.document_chunk_max_tokens == 1_600
                assert settings.document_chunk_mode == "heading_strict"
                g = (await c.get("/api/v1/system/model-config", headers=A)).json()
                assert g["llm_model"] == "test-model-x" and g["search_top_k"] == 5
                assert g["sag_language"] == "en"
                assert g["llm_timeout_ms"] == 45_000 and g["llm_max_retries"] == 3
                assert g["document_chunk_max_tokens"] == 1_600
                assert g["document_chunk_mode"] == "heading_strict"

                # 密钥：设假 key → set=True 且不回显明文
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "llm_base_url": "https://api.302.ai/v1",
                        "llm_api_key": "sk-fake-xyz",
                    },
                )
                assert r.json()["config"]["llm_base_url"] == "https://api.302ai.cn/v1"
                assert r.json()["config"]["llm_api_key_set"] is True
                assert "sk-fake" not in r.text

                # 升级前已配置 302 的用户可在服务端复用旧 Key，一键补齐 MinerU。
                r = await c.post("/api/v1/system/model-config/mineru/302", headers=A)
                assert r.status_code == 200
                assert r.json()["config"]["mineru_base_url"] == "https://api.302ai.cn"
                assert r.json()["config"]["mineru_api_key_set"] is True
                assert "sk-fake" not in r.text
                # 留空提交 → 保留原 key（仍 set），同时更新其他字段
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={"llm_api_key": "", "llm_model": "m2"},
                )
                assert r.json()["config"]["llm_api_key_set"] is True
                assert r.json()["config"]["llm_model"] == "m2"

                # 文档解析配置与密钥同样支持持久化、脱敏和即时生效。
                r = await c.put(
                    "/api/v1/system/model-config",
                    headers=A,
                    json={
                        "document_parser": "auto",
                        "mineru_base_url": "https://mineru.example.test",
                        "mineru_api_key": "sk-mineru-fake",
                        "mineru_version": "2.5",
                        "document_extract_concurrency": 7,
                    },
                )
                parser_config = r.json()["config"]
                assert parser_config["mineru_api_key_set"] is True
                assert parser_config["effective_document_parser"] == "mineru"
                assert parser_config["document_extract_concurrency"] == 7
                assert "sk-mineru-fake" not in r.text

                # 非法值 → 422（Literal / 越界）
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_strategy": "nope"})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"llm_provider": "nope"})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_strategy": "atomic"})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"search_top_k": 999})
                ).status_code == 422
                assert (
                    await c.put("/api/v1/system/model-config", headers=A, json={"document_parser": None})
                ).status_code == 422
                assert (
                    await c.put(
                        "/api/v1/system/model-config",
                        headers=A,
                        json={"document_extract_concurrency": 0},
                    )
                ).status_code == 422
                for invalid in (
                    {"llm_timeout_ms": 999},
                    {"llm_timeout_ms": None},
                    {"llm_max_retries": 11},
                    {"llm_max_retries": None},
                    {"document_chunk_max_tokens": 99},
                    {"document_chunk_max_tokens": None},
                    {"document_chunk_mode": "overlap"},
                    {"document_chunk_mode": None},
                ):
                    assert (await c.put("/api/v1/system/model-config", headers=A, json=invalid)).status_code == 422
                assert (
                    await c.put(
                        "/api/v1/system/model-config",
                        headers=A,
                        json={"document_extract_concurrency": None},
                    )
                ).status_code == 422
    finally:
        async with SessionLocal() as s:
            await s.execute(
                delete(Setting).where(
                    Setting.scope == "global",
                    Setting.key.in_(["model_config", "system_preferences"]),
                )
            )
            await s.commit()
        for key, value in snapshot.items():
            setattr(settings, key, value)
