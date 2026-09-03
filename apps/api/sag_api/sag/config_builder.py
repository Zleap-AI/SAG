"""由 sag 配置装配 zleap-sag 0.8.2 的 `EngineConfig`。

支持信源级覆盖（`overrides`）——目前支持 `language`，未来可扩展 `entity_types` 等。
0.8.2 变更:`storage_mode` 必填;向量库改为显式 VectorConfig 家族
(LanceDB / Elasticsearch / PgVector / OceanBase),不再使用 vector_provider 字符串。
"""

from __future__ import annotations

from typing import Any

from zleap.sag import EngineConfig
from zleap.sag.config import (
    ElasticsearchVectorConfig,
    EmbeddingConfig,
    LLMConfig,
    OceanBaseConnectionConfig,
    OceanBaseVectorConfig,
    PgVectorConfig,
    PostgresConnectionConfig,
    RelationalConfig,
)
from zleap.sag.core.ai.structured import StructuredOutputMode

from sag_api.core.config import Settings

# LLM 未配置时的占位符：允许 EngineConfig 构造 / start() 建 schema（离线路径），
# 真正的 ingest / extract / search 会在运行时因缺少凭证而报错（服务层已前置守卫）。
_PLACEHOLDER = "not-configured"

# SAG 的向量后端取值 → 0.8.2 的 VectorConfig 构造。lancedb 由 EngineConfig 从
# data_dir 自动派生（vector=None），其余需显式连接配置。
_VECTOR_PROVIDERS = frozenset({"lancedb", "es", "pgvector", "oceanbase"})


def _build_vector(settings: Settings) -> Any:
    provider = settings.sag_vector_provider
    if provider == "lancedb":
        return None  # EngineConfig 派生 data_dir/lancedb
    if provider == "es":
        # 0.8.2 要求 hosts;SAG 未提供 ES 地址配置时退回本地默认。
        # TODO(REQ-7/配置):新增 SAG_ES_HOSTS 设置项,生产显式配置。
        return ElasticsearchVectorConfig(hosts=["http://localhost:9200"])
    if provider == "pgvector":
        return PgVectorConfig(
            connection=PostgresConnectionConfig(
                host=settings.sag_pg_host,
                port=settings.sag_pg_port,
                user=settings.sag_pg_user,
                password=settings.sag_pg_password,
                database=settings.sag_pg_database,
            )
        )
    if provider == "oceanbase":
        return OceanBaseVectorConfig(
            connection=OceanBaseConnectionConfig(
                host=settings.sag_pg_host,
                port=2881,
                user=settings.sag_pg_user,
                password=settings.sag_pg_password,
                database=settings.sag_pg_database,
            )
        )
    raise ValueError(f"不支持的向量后端: {provider}")


def _build_relational(settings: Settings) -> RelationalConfig | None:
    provider = settings.sag_relational_provider
    if not provider or provider == "sqlite":
        return None  # EngineConfig 从 data_dir 派生 SQLite
    return RelationalConfig(
        provider=provider,  # postgres / mysql / oceanbase
        host=settings.sag_pg_host,
        port=settings.sag_pg_port,
        user=settings.sag_pg_user,
        password=settings.sag_pg_password,
        database=settings.sag_pg_database,
    )


def _structured_output_mode(settings: Settings) -> StructuredOutputMode:
    """映射显式模式；auto 由 SAG LiteLLM seam 首选 schema 并按能力降级。"""
    mode = settings.llm_structured_output_mode
    if mode == "auto":
        return StructuredOutputMode.JSON_SCHEMA
    return StructuredOutputMode(mode)


def build_engine_config(settings: Settings, *, overrides: dict[str, Any] | None = None) -> EngineConfig:
    overrides = overrides or {}

    llm = LLMConfig(
        api_key=settings.llm_api_key or _PLACEHOLDER,
        model=settings.routed_llm_model,
        provider="litellm",
        base_url=settings.llm_base_url,
        temperature=settings.effective_llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=max(1, (settings.llm_timeout_ms + 999) // 1000),
        max_retries=settings.llm_max_retries,
        structured_output_mode=_structured_output_mode(settings),
    )
    embedding = EmbeddingConfig(
        model=settings.embedding_model,
        base_url=settings.effective_embedding_base_url,
        api_key=settings.effective_embedding_api_key or _PLACEHOLDER,
        # 未配置时保持 None：引擎会从首批真实向量推断存储 schema，
        # 同时避免向不支持 dimensions 参数的 Embedding API 发送该字段。
        dimensions=settings.embedding_dimensions,
    )

    return EngineConfig(
        storage_mode="normal",
        llm=llm,
        embedding=embedding,
        relational=_build_relational(settings),
        vector=_build_vector(settings),
        data_dir=str(overrides.get("data_dir") or settings.data_dir),
        language=overrides.get("language", settings.sag_language),
    )
