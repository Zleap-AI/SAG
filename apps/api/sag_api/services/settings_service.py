"""运行期模型与知识库配置 —— DB 覆盖层叠加在 env 默认（`settings` 单例）之上。

单用户本地示范：把「模型与检索」配置存进 `settings` 表（scope=global, key=model_config）。
启动时与保存后**就地覆盖 `settings` 单例**的相应字段，端点再重建 `LLMClient` / 重置暖引擎，
使配置改动**无需重启即生效**。api_key 明文入库（本地单用户可接受），读取时脱敏（只返回是否已设）。
"""

from __future__ import annotations

from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sag_api.core.config import Settings
from sag_api.core.config import settings as _settings
from sag_api.core.errors import ConfigurationError
from sag_api.core.logging import get_logger
from sag_api.core.model_providers import get_model_provider
from sag_api.db.models import Setting
from sag_api.enums import SEARCH_STRATEGIES, normalize_search_strategy

_SCOPE = "global"
_KEY = "model_config"
_PREFERENCES_KEY = "system_preferences"
log = get_logger("settings")

# 允许运行期覆盖的字段（值已由请求 schema 校验/转型）
_FIELDS = frozenset(
    {
        "llm_provider",
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_temperature",
        "llm_max_tokens",
        "llm_context_window",
        "llm_timeout_ms",
        "llm_max_retries",
        "embedding_model",
        "embedding_base_url",
        "embedding_api_key",
        "embedding_dimensions",
        "document_parser",
        "mineru_base_url",
        "mineru_api_key",
        "mineru_version",
        "document_extract_concurrency",
        "document_chunk_max_tokens",
        "document_chunk_mode",
        "search_strategy",
        "search_top_k",
        "sag_language",
    }
)
_SECRET_FIELDS = frozenset({"llm_api_key", "embedding_api_key", "mineru_api_key"})
_NULLABLE_FIELDS = frozenset({"llm_base_url", "embedding_base_url", "embedding_dimensions", "mineru_base_url"})

_OPENAI_COMPATIBLE = get_model_provider("openai")

QUICK_SETUP_302 = {
    "llm_provider": _OPENAI_COMPATIBLE.id,
    "llm_base_url": _OPENAI_COMPATIBLE.default_base_url,
    "llm_model": _OPENAI_COMPATIBLE.default_model,
    "llm_temperature": _OPENAI_COMPATIBLE.default_temperature,
    "llm_max_tokens": 20_000,
    "llm_context_window": _OPENAI_COMPATIBLE.default_context_window,
    "llm_timeout_ms": 60_000,
    "llm_max_retries": 2,
    "embedding_model": "Qwen/Qwen3-Embedding-4B",
    "embedding_base_url": "https://api.302ai.cn/v1",
    "embedding_dimensions": 1024,
    "document_parser": "auto",
    "mineru_base_url": "https://api.302ai.cn",
    "mineru_version": "2.5",
    "document_extract_concurrency": 5,
    "document_chunk_max_tokens": 1_000,
    "document_chunk_mode": "standard",
    "search_strategy": "vector",
    "search_top_k": 8,
    "sag_language": "zh",
}

_LEGACY_302_BASE_URLS = {
    "https://api.302.ai": "https://api.302ai.cn",
    "https://api.302.ai/v1": "https://api.302ai.cn/v1",
}


async def _load_row(session: AsyncSession, key: str = _KEY) -> Setting | None:
    return await session.scalar(select(Setting).where(Setting.scope == _SCOPE, Setting.key == key))


def _normalize_overrides(overrides: dict) -> dict:
    """清理持久化配置，确保已下线或非法策略不会进入运行时。"""
    normalized = dict(overrides)
    for field in ("llm_base_url", "embedding_base_url", "mineru_base_url"):
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = _LEGACY_302_BASE_URLS.get(value.rstrip("/"), value)
    strategy = normalized.get("search_strategy")
    if strategy == "atomic":
        normalized["search_strategy"] = normalize_search_strategy(strategy)
        log.warning("旧检索策略 atomic 已迁移为精确模式 multi")
    elif strategy is not None and strategy not in SEARCH_STRATEGIES:
        normalized.pop("search_strategy", None)
        log.warning("忽略非法的持久化检索策略：%s", strategy)
    return normalized


async def load_overrides(session: AsyncSession) -> dict:
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    return _normalize_overrides(raw)


async def model_setup_status(session: AsyncSession) -> dict[str, bool]:
    """判断是否需要首次模型配置，不受运行期 DB 覆盖后的 settings 单例干扰。"""
    row = await _load_row(session)
    environment_configured = Settings().llm_configured
    database_configured = bool(row and isinstance(row.value, dict) and row.value.get("llm_api_key"))
    return {
        "required": not environment_configured and not database_configured,
        "environment_configured": environment_configured,
        "database_configured": database_configured,
    }


def apply_overrides(settings: Settings, overrides: dict) -> None:
    """把存储的覆盖值就地写回 settings 单例（请求 schema 已保证类型合法）。"""
    for key, value in _normalize_overrides(overrides).items():
        if key in _FIELDS:
            setattr(settings, key, value)


async def apply_startup_overrides(session_factory: async_sessionmaker) -> None:
    """启动时：把 DB 里的模型配置覆盖到 settings 单例（在构建 LLMClient 之前调用）。"""
    async with session_factory() as session:
        row = await _load_row(session)
        raw = dict(row.value) if row and isinstance(row.value, dict) else {}
        overrides = _normalize_overrides(raw)
        if row is not None and overrides != raw:
            # JSON 列未使用 MutableDict，必须整体重新赋值才能可靠持久化。
            row.value = overrides
            await session.commit()
        apply_overrides(_settings, overrides)
        preferences = await _load_row(session, _PREFERENCES_KEY)
        preference_values = dict(preferences.value) if preferences and isinstance(preferences.value, dict) else {}
        timezone = preference_values.get("timezone")
        if isinstance(timezone, str):
            # Stored values were validated on write. Settings assignment is kept
            # explicit so model configuration and presentation preferences remain separate.
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("忽略非法的持久化时区：%s", timezone)
            else:
                _settings.timezone = timezone


def effective_model_config() -> dict:
    """当前生效的模型配置（读 settings 单例；密钥脱敏为 *_set 布尔）。"""
    return {
        "llm_provider": _settings.llm_provider,
        "llm_base_url": _settings.llm_base_url,
        "llm_model": _settings.llm_model,
        "llm_temperature": _settings.llm_temperature,
        "llm_max_tokens": _settings.llm_max_tokens,
        "llm_context_window": _settings.llm_context_window,
        "llm_timeout_ms": _settings.llm_timeout_ms,
        "llm_max_retries": _settings.llm_max_retries,
        "llm_api_key_set": bool(_settings.llm_api_key),
        "embedding_model": _settings.embedding_model,
        "embedding_base_url": _settings.embedding_base_url,
        "embedding_dimensions": _settings.embedding_dimensions,
        "embedding_api_key_set": bool(_settings.embedding_api_key),
        "document_parser": _settings.document_parser,
        "effective_document_parser": _settings.effective_document_parser,
        "mineru_base_url": _settings.mineru_base_url,
        "mineru_version": _settings.mineru_version,
        "mineru_api_key_set": bool(_settings.mineru_api_key),
        "document_extract_concurrency": _settings.document_extract_concurrency,
        "document_chunk_max_tokens": _settings.document_chunk_max_tokens,
        "document_chunk_mode": _settings.document_chunk_mode,
        "search_strategy": _settings.search_strategy,
        "search_top_k": _settings.search_top_k,
        "sag_language": _settings.sag_language,
    }


def effective_system_preferences() -> dict[str, str]:
    return {"timezone": _settings.timezone}


async def save_system_preferences(session: AsyncSession, patch: dict) -> dict[str, str]:
    row = await _load_row(session, _PREFERENCES_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    timezone = patch.get("timezone")
    if isinstance(timezone, str):
        stored["timezone"] = timezone

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_PREFERENCES_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    if isinstance(stored.get("timezone"), str):
        _settings.timezone = stored["timezone"]
    return effective_system_preferences()


async def save_model_config(session: AsyncSession, patch: dict) -> dict:
    """合并保存模型配置：入库 + 覆盖 settings 单例；返回生效配置（脱敏）。

    约定（配合 `exclude_unset`）：
    - 字段未出现 → 保持不变；
    - 密钥字段值为空 → 忽略（保留原密钥，避免误清空）；空值仅经显式非空覆盖；
    - 可空字段（base_url / dimensions）值为空 → 置 None（清除）。
    """
    row = await _load_row(session)
    raw = dict(row.value) if row and isinstance(row.value, dict) else {}
    stored = _normalize_overrides(raw)

    for key, value in patch.items():
        if key not in _FIELDS:
            continue
        if key in _SECRET_FIELDS:
            if value:  # 仅非空才更新；空/None 保留原值
                stored[key] = str(value)
            continue
        if key in _NULLABLE_FIELDS and (value is None or value == ""):
            stored[key] = None
            continue
        stored[key] = value

    stored = _normalize_overrides(stored)

    if row is None:
        session.add(Setting(scope=_SCOPE, key=_KEY, value=stored))
    else:
        row.value = stored
    await session.commit()

    apply_overrides(_settings, stored)
    return effective_model_config()


async def save_302_quick_setup(session: AsyncSession, api_key: str) -> dict:
    """用单个 302.AI Key 写入生成、向量、MinerU 与快速检索预设。"""
    return await save_model_config(
        session,
        {
            **QUICK_SETUP_302,
            "llm_api_key": api_key,
            "embedding_api_key": api_key,
            "mineru_api_key": api_key,
        },
    )


async def save_302_mineru_setup(session: AsyncSession) -> dict:
    """为已有 302 模型配置复用现有 Key，不把密钥回传给浏览器。"""
    candidates = (
        (_settings.llm_base_url, _settings.llm_api_key),
        (_settings.effective_embedding_base_url, _settings.effective_embedding_api_key),
    )
    for base_url, api_key in candidates:
        parsed = urlparse(base_url or "")
        host = (parsed.hostname or "").lower()
        if host not in {"api.302.ai", "api.302ai.cn"} or not api_key:
            continue
        return await save_model_config(
            session,
            {
                "document_parser": "auto",
                "mineru_base_url": "https://api.302ai.cn",
                "mineru_api_key": api_key,
                "mineru_version": "2.5",
            },
        )
    raise ConfigurationError("未找到可复用的 302.AI 模型 API Key")
