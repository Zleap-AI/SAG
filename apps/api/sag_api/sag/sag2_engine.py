"""zleap-sag2 只读直搜：共用 JEV 连接，在 SAG 关系库上做 BM25 召回与多轮证据评分。

与 ``multi_jev`` 不同，SAG2 不经过每信源的 zleap-sag ``DataEngine``：一个未绑定数据源的
``zleap.sag2.DataEngine`` 连接同一关系库，识别 ``source_chunk`` 后以只读方式检索，
一次请求覆盖全部目标信源并由引擎给出最终顺序。写入、删除仍由 zleap-sag 管理。

首期只支持 SQLite 与 PostgreSQL；直读模式每次检索会扫描所选信源的全部可见段落，
因此这里同时限制并发检索数与范围内段落总数，排队与扫描都计入整次超时。

对 zleap-sag2 内部结构的依赖集中在 ``_sag_schema_present`` 与 ``_count_scope`` 两个
函数里（``engine.store.engine`` 是其关系库连接）；升级 ``zleap-sag2`` 版本时需复核这两处。
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
from sag_api.sag.errors import (
    JEV_AUTH_FAILED_MESSAGE,
    JEV_REQUEST_REJECTED_MESSAGE,
    JEV_UNAVAILABLE_MESSAGE,
)

if TYPE_CHECKING:
    from sag_api.core.config import Settings
    from sag_api.db.models import Source

log = get_logger("sag.sag2")

SAG2_RELATIONAL_PROVIDERS = frozenset({"sqlite", "postgres"})
SAG2_STRATEGY = "sag2"
SAG_CHUNK_TABLE = "source_chunk"


def relational_provider(settings: Settings) -> str:
    return settings.sag_relational_provider or "sqlite"


def jev_connection_issue(settings: Settings) -> tuple[str, str] | None:
    """JEV 为付费服务：地址与 API Key 都保存后，才允许选择任何 JEV 评分模式。

    返回 ``(reason_code, 用户可读文案)``；连接完整时返回 ``None``。
    """
    if not settings.jev_endpoint:
        return ("jev_not_configured", "尚未配置 JEV 精排服务，请先保存服务地址。")
    if not settings.jev_api_key:
        return ("jev_api_key_missing", "JEV 为付费服务，请先在设置中填写 API Key。")
    return None


def sag2_relational_issue(settings: Settings) -> tuple[str, str] | None:
    """SAG2 首期只读 SQLite / PostgreSQL；其他关系库返回不可用原因。"""
    provider = relational_provider(settings)
    if provider in SAG2_RELATIONAL_PROVIDERS:
        return None
    return (
        "sag2_relational_unsupported",
        f"SAG2 证据检索首期仅支持 SQLite 与 PostgreSQL 关系库，当前为 {provider}。",
    )


def jev_connection_ready(settings: Settings) -> bool:
    return jev_connection_issue(settings) is None


def sag2_relational_supported(settings: Settings) -> bool:
    return sag2_relational_issue(settings) is None


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

    for issue in (sag2_relational_issue(settings), jev_connection_issue(settings)):
        if issue is not None:
            code, message = issue
            raise ConfigurationError(message, code=code, stage=ErrorStage.RETRIEVE)
    if relational_provider(settings) == "sqlite":
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


def _base_stats(*, sources: int, requested_sources: int, source_limit_applied: bool, candidates: int) -> dict[str, Any]:
    """所有 SAG2 结果共有的 stats 字段；SAG2 没有粗排降级，``fallback_used`` 恒为 False。"""
    return {
        "sources": sources,
        "sources_requested": requested_sources,
        "source_limit_applied": source_limit_applied,
        "candidates": candidates,
        "requested_strategy": SAG2_STRATEGY,
        "effective_strategy": SAG2_STRATEGY,
        "fallback_used": False,
        "ranking": {"method": "jev", "fallback_reason": None},
    }


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
    stats = _base_stats(
        sources=len(data_source_ids),
        requested_sources=requested_sources,
        source_limit_applied=source_limit_applied,
        candidates=len(sections),
    )
    stats["jev_usage"] = {
        "requests": usage.get("requests"),
        "judgments": usage.get("judgments"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    stats["sag2"] = {
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
    }
    return SearchOutcome(query=getattr(result, "query", None) or query, sections=sections, stats=stats)


class _AdmissionGate:
    """可在线调整上限的并发闸门：在途检索继续持有名额，新检索按当前上限排队。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def active(self) -> int:
        return self._active

    async def set_limit(self, limit: int) -> None:
        async with self._condition:
            self._limit = max(1, limit)
            self._condition.notify_all()

    async def __aenter__(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def __aexit__(self, *_exc: object) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    async def drain(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active == 0)


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
        self._gate = _AdmissionGate(settings.sag2_max_concurrent_searches)

    @property
    def started(self) -> bool:
        return self._engine is not None

    @property
    def stale(self) -> bool:
        """当前引擎是否已不匹配配置；下次检索会按新配置重建。"""
        return self._engine is not None and self._fingerprint != _fingerprint(self._settings)

    async def refresh(self, settings: Settings) -> None:
        """配置发布后调用：新的并发上限立即生效；连接或存储变化后由下次检索惰性重建。

        在途检索持有旧引擎的本地引用，不在这里关闭它，避免打断正在进行的评分。
        """
        self._settings = settings
        await self._gate.set_limit(settings.sag2_max_concurrent_searches)

    async def aclose(self) -> None:
        """等待在途检索结束后关闭引擎；与 ``EngineManager._close_all_slots`` 等待空闲的语义一致。"""
        await self._gate.drain()
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

        config = build_sag2_config(settings)
        not_ready = ConfigurationError(
            "知识库尚未初始化，暂无可检索的段落", code="sag2_storage_not_ready", stage=ErrorStage.RETRIEVE,
        )
        if relational_provider(settings) == "sqlite" and not Path(settings.data_dir, "sag.db").is_file():
            raise not_ready
        scorer = self._scorer_factory(config) if self._scorer_factory is not None else None
        engine = DataEngine(config, scorer=scorer, health_check=False)
        try:
            # 空库会让 zleap-sag2 建立自己的业务表；只在已有 SAG 段落表时才启动只读模式。
            if not await self._sag_schema_present(engine):
                raise not_ready
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
            return SearchOutcome(query=query, sections=[], stats=_base_stats(
                sources=0, requested_sources=requested_sources, source_limit_applied=source_limit_applied, candidates=0,
            ))
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

        async with self._gate:
            engine = await self.engine()
            settings = self._settings
            scope_chunks = await self._count_scope(engine, data_source_ids)
            limit = int(settings.sag2_max_scope_chunks)
            if limit and scope_chunks > limit:
                raise ValidationError(
                    f"检索范围过大：包含 {scope_chunks:,} 个段落，超过 SAG2 证据检索上限 {limit:,}，请缩小信源范围",
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
    async def _sag_schema_present(engine: Any) -> bool:
        """SAG 的段落表是否存在；zleap-sag2 据此决定进入只读直搜模式。"""
        from sqlalchemy import inspect

        async with engine.store.engine.connect() as connection:
            return bool(await connection.run_sync(lambda sync: inspect(sync).has_table(SAG_CHUNK_TABLE)))

    @staticmethod
    async def _count_scope(engine: Any, data_source_ids: list[str]) -> int:
        """范围内段落行数（含待清理的历史版本），用于扫描成本护栏。"""
        from sqlalchemy import column, func, select, table

        chunks = table(SAG_CHUNK_TABLE, column("data_source_id"))
        statement = select(func.count()).select_from(chunks).where(chunks.c.data_source_id.in_(data_source_ids))
        async with engine.store.engine.connect() as connection:
            return int(await connection.scalar(statement) or 0)

    @staticmethod
    def _translate(error: Exception) -> Exception:
        """把 zleap-sag2 的异常翻译为带 layer/stage 的领域错误，文案与 ``multi_jev`` 一致；凭据与响应体不进消息。"""
        from zleap.sag2 import JevError, SagError
        from zleap.sag2.exceptions import ConfigError as Sag2ConfigError

        if isinstance(error, (ConfigurationError, ServiceUnavailableError, ValidationError, UpstreamError)):
            return error
        if isinstance(error, JevError):
            status = error.details.get("status") if isinstance(error.details, dict) else None
            if status in (401, 403):
                return ConfigurationError(JEV_AUTH_FAILED_MESSAGE, layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE)
            # 408/429 与 5xx 由 zleap-sag2 自行重试，耗尽后以“重试用尽”上抛；其余 4xx 是请求本身被拒绝。
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                return ValidationError(JEV_REQUEST_REJECTED_MESSAGE, layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE)
            return ServiceUnavailableError(JEV_UNAVAILABLE_MESSAGE, layer=ErrorLayer.ENGINE, stage=ErrorStage.RETRIEVE)
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
    "jev_connection_issue",
    "jev_connection_ready",
    "outcome_from_result",
    "sag2_relational_issue",
    "sag2_relational_supported",
]
