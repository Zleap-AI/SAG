"""zleap-sag2 只读直搜：共用 JEV 连接，在 SAG 关系库上做 BM25 召回与多轮证据评分。

与 ``multi_jev`` 不同，SAG2 不经过每信源的 zleap-sag ``DataEngine``：一个未绑定数据源的
``zleap.sag2.DataEngine`` 连接同一关系库，识别 ``source_chunk`` 后以只读方式检索，
一次请求覆盖全部目标信源并由引擎给出最终顺序。写入、删除仍由 zleap-sag 管理。

首期只支持 SQLite 与 PostgreSQL；直读模式每次检索会扫描所选信源的全部可见段落，
因此这里同时限制并发检索数与范围内段落总数，排队与扫描都计入整次超时。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import (
    ConfigurationError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
)
from sag_api.core.logging import get_logger
from sag_api.sag.dto import RetrievedSection, SearchOutcome

if TYPE_CHECKING:
    from sag_api.core.config import Settings
    from sag_api.db.models import Source

log = get_logger("sag.sag2")

SAG2_RELATIONAL_PROVIDERS = frozenset({"sqlite", "postgres"})
SAG2_STRATEGY = "sag2"


def relational_provider(settings: Settings) -> str:
    return settings.sag_relational_provider or "sqlite"


def sag2_relational_supported(settings: Settings) -> bool:
    return relational_provider(settings) in SAG2_RELATIONAL_PROVIDERS


def jev_connection_ready(settings: Settings) -> bool:
    """JEV 为付费服务：地址与 API Key 都保存后，才允许选择任何 JEV 评分模式。"""
    return bool(settings.jev_endpoint and settings.jev_api_key)


def _fingerprint(settings: Settings) -> tuple[Any, ...]:
    """决定是否需要重建只读引擎的配置子集；检索参数在每次请求时读取，不在此列。"""
    return (
        settings.jev_endpoint,
        settings.jev_model,
        settings.jev_api_key,
        relational_provider(settings),
        settings.data_dir,
        settings.sag_pg_host,
        settings.sag_pg_port,
        settings.sag_pg_user,
        settings.sag_pg_password,
        settings.sag_pg_database,
        settings.sag2_decision_timeout_seconds,
        settings.sag2_decision_max_concurrency,
    )


def build_sag2_config(settings: Settings):
    """由 SAG 运行时配置装配 zleap-sag2 的 ``EngineConfig``（同一关系库 + 共用 JEV 连接）。"""
    from zleap.sag2 import EngineConfig, JevConfig, RelationalConfig

    provider = relational_provider(settings)
    if provider not in SAG2_RELATIONAL_PROVIDERS:
        raise ConfigurationError(
            f"SAG2 证据检索首期仅支持 SQLite 与 PostgreSQL 关系库，当前为 {provider}",
            code="sag2_relational_unsupported",
            stage=ErrorStage.RETRIEVE,
        )
    if not settings.jev_endpoint:
        raise ConfigurationError(
            "尚未配置 JEV 精排服务，请先保存服务地址。", code="jev_not_configured", stage=ErrorStage.RETRIEVE,
        )
    if not settings.jev_api_key:
        raise ConfigurationError(
            "JEV 为付费服务，请先在设置中填写 API Key。", code="jev_api_key_missing", stage=ErrorStage.RETRIEVE,
        )
    if provider == "sqlite":
        # zleap-sag 在 data_dir 下派生同名 SQLite 文件；运行时已把 data_dir 替换为活动目录。
        relational = RelationalConfig(provider="sqlite", path=str(Path(settings.data_dir) / "sag.db"))
    else:
        relational = RelationalConfig(
            provider="postgres",
            host=settings.sag_pg_host,
            port=settings.sag_pg_port,
            user=settings.sag_pg_user,
            password=settings.sag_pg_password,
            database=settings.sag_pg_database,
        )
    jev = JevConfig(
        endpoint=settings.jev_endpoint,
        model=settings.jev_model,
        api_key=settings.jev_api_key,
        timeout_seconds=settings.sag2_decision_timeout_seconds,
        max_concurrency=settings.sag2_decision_max_concurrency,
    )
    return EngineConfig(relational=relational, jev=jev)


def outcome_from_result(
    result: Any,
    *,
    query: str,
    data_source_ids: list[str],
    requested_sources: int,
    source_limit_applied: bool,
    scope_chunks: int,
) -> SearchOutcome:
    """把 zleap-sag2 的 ``SearchResult`` 映射为业务 DTO，保留引擎顺序与原始 JEV 分数。"""
    sections = [
        RetrievedSection(
            chunk_id=hit.chunk_id or hit.id,
            heading=(hit.heading or hit.title or "").strip(),
            content=(hit.content or "").strip(),
            score=float(hit.score or 0.0),
            rank=index,
            source_id=hit.source_id,
            source_config_id=hit.data_source_id,
            metadata={"score_source": "jev", "hop": int(hit.hop or 0)},
        )
        for index, hit in enumerate(result.chunks)
    ]
    usage = dict(getattr(result, "usage", None) or {})
    trace = getattr(result, "trace", None) or ()
    stats: dict[str, Any] = {
        "sources": len(data_source_ids),
        "sources_requested": requested_sources,
        "source_limit_applied": source_limit_applied,
        "candidates": len(sections),
        "requested_strategy": SAG2_STRATEGY,
        "effective_strategy": SAG2_STRATEGY,
        "fallback_used": False,
        "ranking": {"method": "jev", "fallback_reason": None},
        "jev_usage": {
            "requests": usage.get("requests"),
            "judgments": usage.get("judgments"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
        "sag2": {
            "elapsed_ms": round(float(getattr(result, "elapsed_ms", 0.0) or 0.0), 2),
            "local_ms": round(float(getattr(result, "local_ms", 0.0) or 0.0), 2),
            "jev_ms": round(float(getattr(result, "jev_ms", 0.0) or 0.0), 2),
            "snapshot_revision": getattr(result, "snapshot_revision", None),
            "scope_chunks": scope_chunks,
            "hops": [
                {"stage": item.get("stage"), "hop": item.get("hop"), "count": item.get("count")}
                for item in trace
                if isinstance(item, dict)
            ],
        },
    }
    return SearchOutcome(query=getattr(result, "query", None) or query, sections=sections, stats=stats)


def _empty_outcome(query: str, *, requested_sources: int, source_limit_applied: bool) -> SearchOutcome:
    return SearchOutcome(query=query, sections=[], stats={
        "sources": 0,
        "sources_requested": requested_sources,
        "source_limit_applied": source_limit_applied,
        "candidates": 0,
        "requested_strategy": SAG2_STRATEGY,
        "effective_strategy": SAG2_STRATEGY,
        "fallback_used": False,
        "ranking": {"method": "jev", "fallback_reason": None},
    })


class Sag2EngineHolder:
    """按配置指纹懒建、复用、重建一个只读 zleap-sag2 引擎，并限制并发检索数。

    ``scorer_factory`` 仅供测试注入离线评分器；缺省由引擎按 ``JevConfig`` 创建真实客户端。
    """

    def __init__(self, settings: Settings, *, scorer_factory: Callable[[Any], Any] | None = None) -> None:
        self._settings = settings
        self._scorer_factory = scorer_factory
        self._engine: Any | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()
        self._semaphore_size = max(1, settings.sag2_max_concurrent_searches)
        self._semaphore = asyncio.Semaphore(self._semaphore_size)

    @property
    def started(self) -> bool:
        return self._engine is not None

    async def refresh(self, settings: Settings) -> None:
        """配置发布后调用：连接或存储参数变化则关闭当前引擎，下次检索按新配置重建。"""
        self._settings = settings
        size = max(1, settings.sag2_max_concurrent_searches)
        if size != self._semaphore_size:
            self._semaphore_size = size
            self._semaphore = asyncio.Semaphore(size)
        if self._engine is not None and self._fingerprint != _fingerprint(settings):
            async with self._lock:
                await self._close()

    async def aclose(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        engine, self._engine, self._fingerprint = self._engine, None, None
        if engine is None:
            return
        try:
            await engine.aclose()
        except Exception as error:  # noqa: BLE001 - best-effort shutdown
            log.warning("关闭 SAG2 只读引擎失败：%s", error)

    async def engine(self) -> Any:
        async with self._lock:
            fingerprint = _fingerprint(self._settings)
            if self._engine is not None and self._fingerprint == fingerprint:
                return self._engine
            await self._close()
            self._engine = await self._start(self._settings)
            self._fingerprint = fingerprint
            return self._engine

    async def _start(self, settings: Settings) -> Any:
        from zleap.sag2 import DataEngine

        # 0.1.0 没有公开"是否已有 SAG 库"的探测接口；锁定精确版本后复用其内部检测，
        # 避免在空库上误建 sag2 原生业务表。
        from zleap.sag2._sag1 import Sag1Reader

        config = build_sag2_config(settings)
        if relational_provider(settings) == "sqlite" and not Path(settings.data_dir, "sag.db").is_file():
            raise ConfigurationError(
                "知识库尚未初始化，暂无可检索的段落", code="sag2_storage_not_ready", stage=ErrorStage.RETRIEVE,
            )
        scorer = self._scorer_factory(config) if self._scorer_factory is not None else None
        engine = DataEngine(config, scorer=scorer, health_check=False)
        try:
            async with engine.store.engine.connect() as connection:
                detected = await connection.run_sync(Sag1Reader.detect)
            if detected is None:
                raise ConfigurationError(
                    "知识库尚未初始化，暂无可检索的段落", code="sag2_storage_not_ready", stage=ErrorStage.RETRIEVE,
                )
            await engine.start()
        except BaseException:
            await engine.aclose()
            raise
        log.info("SAG2 只读引擎已启动 provider=%s", relational_provider(settings))
        return engine

    async def search(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        top_k: int,
        requested_sources: int,
        source_limit_applied: bool,
        source_ids_by_config: dict[str, tuple[str, ...]] | None,
    ) -> SearchOutcome:
        """一次只读检索覆盖全部目标信源；失败直接上抛，不回退其他策略。"""
        data_source_ids = [scid for scid, _ in targets]
        if source_ids_by_config is not None:
            data_source_ids = [scid for scid in data_source_ids if source_ids_by_config.get(scid)]
        if not data_source_ids:
            return _empty_outcome(query, requested_sources=requested_sources, source_limit_applied=source_limit_applied)
        source_ids = (
            None
            if source_ids_by_config is None
            else tuple(sorted({sid for scid in data_source_ids for sid in source_ids_by_config[scid]}))
        )
        timeout = max(1.0, float(self._settings.sag2_search_timeout_seconds))
        try:
            result, scope_chunks = await asyncio.wait_for(
                self._run(query, data_source_ids, source_ids, top_k), timeout,
            )
        except TimeoutError as error:
            raise ServiceUnavailableError(
                "SAG2 检索超时，请缩小信源范围或稍后重试", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
            ) from error
        return outcome_from_result(
            result,
            query=query,
            data_source_ids=data_source_ids,
            requested_sources=requested_sources,
            source_limit_applied=source_limit_applied,
            scope_chunks=scope_chunks,
        )

    async def _run(
        self,
        query: str,
        data_source_ids: list[str],
        source_ids: tuple[str, ...] | None,
        top_k: int,
    ) -> tuple[Any, int]:
        from zleap.sag2 import SearchOptions, SearchRequest, SearchScope

        async with self._semaphore:
            engine = await self.engine()
            settings = self._settings
            scope_chunks = await self._count_scope(engine, data_source_ids)
            limit = int(settings.sag2_max_scope_chunks)
            if limit and scope_chunks > limit:
                raise ValidationError(
                    f"检索范围包含 {scope_chunks:,} 个段落，超过 SAG2 证据检索上限 {limit:,}，请缩小信源范围",
                    layer=ErrorLayer.ENGINE,
                    stage=ErrorStage.RETRIEVE,
                )
            scope_values: dict[str, Any] = {"data_source_ids": tuple(data_source_ids)}
            if source_ids is not None:
                scope_values["source_ids"] = source_ids
            request = SearchRequest(
                query=query,
                scope=SearchScope(**scope_values),
                options=SearchOptions(
                    candidate_k=settings.sag2_candidate_k,
                    seed_k=settings.sag2_seed_k,
                    max_hops=settings.sag2_max_hops,
                    expansion_term_limit=settings.sag2_expansion_term_limit,
                    top_k=max(1, min(int(top_k), 1000)),
                ),
            )
            started = time.perf_counter()
            try:
                result = await engine.search(request)
            except Exception as error:  # noqa: BLE001 - translated below
                raise self._translate(error) from error
            log.info(
                "SAG2 检索完成 sources=%d scope_chunks=%d hits=%d elapsed=%.0fms",
                len(data_source_ids), scope_chunks, len(result.chunks), (time.perf_counter() - started) * 1000,
            )
            return result, scope_chunks

    @staticmethod
    async def _count_scope(engine: Any, data_source_ids: list[str]) -> int:
        """范围内段落行数（含待清理的历史版本），用于扫描成本护栏。"""
        from sqlalchemy import func, select

        reader = getattr(engine.store, "sag1_reader", None)
        table = reader.tables.get("source_chunk") if reader is not None else None
        if table is None:
            return 0
        async with engine.store.engine.connect() as connection:
            statement = select(func.count()).select_from(table).where(table.c.data_source_id.in_(data_source_ids))
            return int(await connection.scalar(statement) or 0)

    @staticmethod
    def _translate(error: Exception) -> Exception:
        """把 zleap-sag2 的异常翻译为带 layer/stage 的领域错误；凭据与响应体不进消息。"""
        from zleap.sag2 import JevError, SagError
        from zleap.sag2.exceptions import ConfigError as Sag2ConfigError

        if isinstance(error, (ConfigurationError, ServiceUnavailableError, ValidationError, UpstreamError)):
            return error
        if isinstance(error, JevError):
            status = error.details.get("status") if isinstance(error.details, dict) else None
            if status in (401, 403):
                return ConfigurationError(
                    "JEV 鉴权失败，请检查服务地址和 API Key", code="jev_auth_failed",
                    layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
                )
            if isinstance(status, int) and 400 <= status < 500:
                return ValidationError(
                    "JEV 评分请求被拒绝，请检查模型和候选内容限制", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
                )
            return ServiceUnavailableError(
                "JEV 评分服务暂不可用，请稍后重试", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
            )
        if isinstance(error, Sag2ConfigError):
            return ConfigurationError(
                "SAG2 证据检索配置无效，请检查 JEV 服务与关系库设置", code=str(error.code),
                layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
            )
        if isinstance(error, SagError):
            return UpstreamError(
                "SAG2 检索失败，请稍后重试", code=str(error.code), layer=ErrorLayer.ENGINE,
                stage=ErrorStage.RETRIEVE, retryable=bool(error.retryable),
            )
        if isinstance(error, TimeoutError):
            return ServiceUnavailableError(
                "SAG2 检索超时，请缩小信源范围或稍后重试", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE,
            )
        return UpstreamError("SAG2 检索失败，请稍后重试", layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE)


__all__ = [
    "SAG2_RELATIONAL_PROVIDERS",
    "SAG2_STRATEGY",
    "Sag2EngineHolder",
    "build_sag2_config",
    "jev_connection_ready",
    "outcome_from_result",
    "sag2_relational_supported",
]
