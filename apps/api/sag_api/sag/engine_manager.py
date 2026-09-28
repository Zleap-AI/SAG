"""EngineManager —— 管理 zleap-sag `DataEngine` 的生命周期与调用。

每个信源（source_config_id）对应一个 `DataEngine` 实例（引擎「一实例一源」的语义）。
引擎按需构造并缓存；每源一把锁串行化该源上的读写。生命周期读写闸门允许
已构造引擎跨源并发；文档处理使用独立 loader/extractor 同源并发。创建、逐出或
关闭引擎时会等待所有在途操作结束。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from zleap.sag import DataEngine

from sag_api.core.config import Settings
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.logging import get_logger
from sag_api.enums import (
    SEARCH_STRATEGIES,
    SEARCH_STRATEGY_REQUIREMENTS,
    normalize_search_strategy,
)
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.content_reader import ContentReader
from sag_api.sag.dto import (
    EntityInfo,
    ProcessCheckpoint,
    ProcessOutcome,
    RetrievedSection,
    SearchOutcome,
    SourceGraphInfo,
    UniverseExpansionInfo,
    UniverseSourceStatsInfo,
    UniverseTimelineInfo,
)
from sag_api.sag.engine_access import EngineAccess
from sag_api.sag.errors import map_sag_errors
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.sag.search_reader import SearchReader
from sag_api.sag.universe_cursor import (
    decode_universe_cursor,
    encode_universe_cursor,
)
from sag_api.sag.universe_reader import UniverseReader

if TYPE_CHECKING:
    from sag_api.db.models import Source

log = get_logger("sag")

StageCallback = Callable[[str], Awaitable[None]]
CheckpointCallback = Callable[[ProcessCheckpoint], Awaitable[None]]
PauseCheck = Callable[[], Awaitable[bool]]

_SQLITE_LOCK_RETRIES = 4


async def _commit_with_sqlite_lock_retry(session: Any, pending: Any | None = None) -> None:
    """Commit one SQLite write through short-lived writer contention.

    PostgreSQL and non-lock failures are never retried. After a failed commit,
    SQLAlchemy rolls pending inserts out of the unit of work, so callers may pass
    the row that must be re-added before the next attempt.
    """
    from sqlalchemy.exc import OperationalError

    for attempt in range(_SQLITE_LOCK_RETRIES):
        try:
            await session.commit()
            return
        except OperationalError as error:
            locked = "database is locked" in str(error).lower()
            if not locked or attempt == _SQLITE_LOCK_RETRIES - 1:
                raise
            await session.rollback()
            if pending is not None:
                session.add(pending)
            await asyncio.sleep(0.08 * (2**attempt))


class _DocumentAdmissionYielded(Exception):
    """A processor waiting to enter the engine yielded to a control intent."""


def _encode_universe_cursor(payload: dict[str, Any], secret: str) -> str:
    return encode_universe_cursor(payload, secret)


def _decode_universe_cursor(value: str, secret: str) -> dict[str, Any]:
    return decode_universe_cursor(value, secret)


@dataclass
class _Slot:
    engine: DataEngine
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    concurrent_allowed: asyncio.Event = field(default_factory=asyncio.Event)
    concurrent_users: int = 0
    maintenance_requests: int = 0
    last_used: float = field(default_factory=time.monotonic)
    closing: bool = False

    def __post_init__(self) -> None:
        self.idle.set()
        self.concurrent_allowed.set()


class _EngineLifecycleGate:
    """Writer-preferring async gate around zleap-sag's shared runtime reset."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        acquired = False
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
                acquired = True
            finally:
                self._waiting_writers -= 1
                if not acquired:
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


class EngineManager:
    supports_document_source_exclusions = True

    def __init__(self, settings: Settings):
        self._settings = settings
        self._slots: dict[str, _Slot] = {}
        self._create_lock = asyncio.Lock()
        # SQLite permits one writer at a time.  Keep local document mutations
        # serialized across source slots; server databases retain full concurrency.
        self._sqlite_document_mutation_lock = asyncio.Lock()
        self._lifecycle_gate = _EngineLifecycleGate()
        self._cache_size = max(1, settings.engine_cache_size)
        self._schema_ready = False
        self._universe_indexes_ready = False
        # 读侧协作者只依赖 EngineAccess 暴露的四个能力，不反向引用本管理器。
        self._access = EngineAccess(self)
        self._universe = UniverseReader(self._access)
        self._content = ContentReader(self._access)
        self._search = SearchReader(self._access)

    async def _relational_session_factory(self, source_config_id: str, source: Source | None = None) -> Any:
        """0.8.2 起 zleap 无全局会话工厂;经引擎注入的 RelationalStore 访问关系库。"""
        slot = await self._slot(source_config_id, source)
        return slot.engine.resources.relational.session_factory()

    async def _vector_store(self, source_config_id: str, source: Source | None = None) -> Any:
        """0.8.2 起向量访问经引擎注入的 VectorStore(替代 get_vector_client)。"""
        slot = await self._slot(source_config_id, source)
        return slot.engine.resources.vector

    async def get_sag_embedding(self, source_config_id: str, source: Source | None = None) -> Any:
        """0.8.2 起 embedding 经引擎注入(替代 get_embedding_client)。"""
        slot = await self._slot(source_config_id, source)
        return slot.engine.resources.embedding

    async def _ensure_universe_query_indexes(self, engine: Any) -> None:
        """Best-effort composite indexes for bounded timeline and neighbor reads."""
        if self._universe_indexes_ready:
            return
        self._universe_indexes_ready = True
        try:
            from sqlalchemy import Index, func
            from zleap.sag.db.models import EventEntity, SourceEvent

            specs = (
                (
                    "idx_universe_event_timeline",
                    SourceEvent.data_source_id,
                    SourceEvent.start_time,
                    SourceEvent.created_time,
                    SourceEvent.id,
                ),
                (
                    "idx_universe_event_category_timeline",
                    SourceEvent.data_source_id,
                    SourceEvent.category,
                    SourceEvent.start_time,
                    SourceEvent.created_time,
                    SourceEvent.id,
                ),
                (
                    "idx_universe_entity_event_timeline",
                    EventEntity.entity_id,
                    EventEntity.created_time,
                    EventEntity.weight,
                    EventEntity.event_id,
                ),
                (
                    "idx_universe_event_entity_weight",
                    EventEntity.event_id,
                    EventEntity.weight,
                    EventEntity.entity_id,
                ),
            )

            def create_missing(sync_connection) -> None:  # noqa: ANN001
                dialect = sync_connection.dialect.name
                if dialect in {"sqlite", "postgresql"}:
                    preparer = sync_connection.dialect.identifier_preparer
                    quote = preparer.quote
                    for name, *columns in specs:
                        table = preparer.format_table(columns[0].table)
                        column_names = ", ".join(quote(column.name) for column in columns)
                        sync_connection.exec_driver_sql(
                            f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {table} ({column_names})"
                        )

                    table = quote(SourceEvent.__table__.name)
                    source = quote(SourceEvent.data_source_id.name)
                    category = quote(SourceEvent.category.name)
                    start = quote(SourceEvent.start_time.name)
                    created = quote(SourceEvent.created_time.name)
                    event_id = quote(SourceEvent.id.name)
                    effective_time = f"COALESCE({start}, {created})"
                    expression_indexes = (
                        (
                            "idx_universe_event_effective_timeline",
                            f"{source}, {effective_time}, {event_id}",
                        ),
                        (
                            "idx_universe_event_category_effective_timeline",
                            f"{source}, {category}, {effective_time}, {event_id}",
                        ),
                    )
                    for name, columns in expression_indexes:
                        sync_connection.exec_driver_sql(
                            f"CREATE INDEX IF NOT EXISTS {quote(name)} ON {table} ({columns})"
                        )
                else:
                    tables = {SourceEvent.__table__, EventEntity.__table__}
                    for name, *columns in specs:
                        index = next(
                            (candidate for table in tables for candidate in table.indexes if candidate.name == name),
                            None,
                        )
                        if index is None:
                            index = Index(name, *columns)
                        index.create(sync_connection, checkfirst=True)

                    event_time = func.coalesce(
                        SourceEvent.start_time,
                        SourceEvent.created_time,
                    )
                    Index(
                        "idx_universe_event_effective_timeline",
                        SourceEvent.data_source_id,
                        event_time,
                        SourceEvent.id,
                    ).create(sync_connection, checkfirst=True)
                    Index(
                        "idx_universe_event_category_effective_timeline",
                        SourceEvent.data_source_id,
                        SourceEvent.category,
                        event_time,
                        SourceEvent.id,
                    ).create(sync_connection, checkfirst=True)

            async with engine.resources.relational.engine().begin() as connection:
                await connection.run_sync(create_missing)
        except Exception:  # noqa: BLE001 - indexes are an optimization, never availability
            log.exception("创建知识宇宙查询索引失败，继续使用现有索引")

    # 门面策略(sag_api 对外) → zleap engine 实际接收的策略名。
    # 门面提供更细粒度的可选项(如 multi_es_fast/precise),而 zleap 上游 mode
    # 默认已经是 fast;此处保留翻译点是为了给后续 precise 变体和 telemetry 区分留位置。
    _FACADE_TO_ZLEAP_STRATEGY: dict[str, str] = {
        "vector": "vector",
        "multi": "full_expand",
        "multi_es_fast": "pruned_expand_rff",
    }

    _VECTOR_PROVIDER_LEXICAL_SUPPORT: dict[str, bool] = {
        "lancedb": True,
        "es": True,
        "pgvector": False,
        "oceanbase": False,
    }

    def _vector_provider_supports_lexical(self) -> bool:
        return self._VECTOR_PROVIDER_LEXICAL_SUPPORT.get(self._settings.sag_vector_provider, False)

    def _strategy_missing_capabilities(self, strategy: str) -> tuple[str, ...]:
        requirements = SEARCH_STRATEGY_REQUIREMENTS.get(strategy, frozenset())
        missing: list[str] = []
        for capability in requirements:
            if capability == "lexical_search" and not self._vector_provider_supports_lexical():
                missing.append(capability)
        return tuple(missing)

    def _effective_search_strategy(self, requested: str | None) -> str:
        """校验请求的门面策略。未识别或能力不满足时按配置策略回退,再兜底 vector。"""
        raw = requested or self._settings.search_strategy
        strategy = normalize_search_strategy(raw)
        if strategy in SEARCH_STRATEGIES:
            missing = self._strategy_missing_capabilities(strategy)
            if not missing:
                if strategy != raw:
                    log.info("旧检索策略 %s 已按精确模式 multi 执行", raw)
                return strategy
            log.warning(
                "策略 %s 所需能力缺失(%s, provider=%s),按后台默认策略回退",
                strategy,
                ",".join(missing),
                self._settings.sag_vector_provider,
            )
        else:
            log.warning("忽略不支持的检索策略 %s", raw)
        fallback = normalize_search_strategy(self._settings.search_strategy)
        if fallback not in SEARCH_STRATEGIES or self._strategy_missing_capabilities(fallback):
            fallback = "vector"
        return fallback

    def _zleap_engine_strategy(self, facade_strategy: str) -> str:
        """把门面策略翻译成 zleap DataEngine.search 认识的名字。"""
        return self._FACADE_TO_ZLEAP_STRATEGY.get(facade_strategy, facade_strategy)

    @classmethod
    def _capability_disabled_reason(cls, capability: str, provider: str) -> tuple[str, str] | None:
        """给定缺失的能力名 + 当前 provider,产出 (reason_code, 用户可读文案)。"""
        if capability == "lexical_search":
            return (
                "vector_provider_lacks_lexical",
                f"当前向量存储 ({provider}) 不支持 BM25 词法检索,无法启用 multi_es 系列策略。",
            )
        return None

    @classmethod
    def strategies_capability_report(cls, settings: Settings) -> dict[str, Any]:
        """探测每个门面策略在当前部署下是否可用,供 /capabilities 直接透传给前端。

        无需持有 EngineManager 实例——所有判据只来自 settings。
        """
        provider = settings.sag_vector_provider
        supports_lexical = cls._VECTOR_PROVIDER_LEXICAL_SUPPORT.get(provider, False)
        enabled: list[str] = []
        disabled: dict[str, dict[str, str]] = {}
        for strategy in sorted(SEARCH_STRATEGIES):
            requirements = SEARCH_STRATEGY_REQUIREMENTS.get(strategy, frozenset())
            missing: list[tuple[str, str]] = []
            for capability in requirements:
                if capability == "lexical_search" and not supports_lexical:
                    reason = cls._capability_disabled_reason(capability, provider)
                    if reason is not None:
                        missing.append(reason)
            if missing:
                # 只呈现第一个原因即可满足 UI 灰置 + tooltip 展示。
                reason_code, message = missing[0]
                disabled[strategy] = {"reason": reason_code, "message": message}
            else:
                enabled.append(strategy)
        return {"enabled": enabled, "disabled": disabled}

    def _config_for(self, source: Source | None) -> Any:
        overrides = None
        if source is not None and source.config:
            overrides = source.config.get("engine")
        return build_engine_config(self._settings, overrides=overrides)

    async def _ensure_engine_schema(self, engine: DataEngine) -> None:
        if self._schema_ready:
            return

        from sqlalchemy.exc import SQLAlchemyError

        from sag_api.core.errors import UpstreamError

        try:
            with map_sag_errors(stage=ErrorStage.PERSIST):
                await engine.init_schema()
        except SQLAlchemyError as error:
            log.exception("知识引擎数据库结构初始化失败")
            raise UpstreamError(
                "信源引擎初始化失败，请稍后重试",
                layer=ErrorLayer.STORE,
                stage=ErrorStage.PERSIST,
            ) from error
        self._schema_ready = True

    async def _ensure_source_config(
        self,
        source_config_id: str,
        source: Source | None = None,
        engine: Any | None = None,
    ) -> None:
        """Ensure the zleap-sag parent row exists before derived data is written.

        0.8.2 把 0.7.1 的 ``SourceConfig`` 换成 ``DataSource``,且无全局会话工厂;
        这里经注入的 engine 访问关系库。SAG 专属的 octx 向量复用元数据
        (原 target_config)改存 SAG 元库 ``Source.config``,不再写入 zleap 库
        (迁移注记:向量复用信任暂降级为关闭,导出走向量重建的安全路径)。
        """
        from sqlalchemy.exc import IntegrityError, SQLAlchemyError
        from zleap.sag.db import DataSource

        effective_engine = engine
        if effective_engine is None:
            slot = await self._slot(source_config_id, source)
            effective_engine = slot.engine

        name = str(getattr(source, "name", "") or f"sag-{source_config_id[-8:]}")[:100]
        description = str(getattr(source, "description", "") or "created by sag EngineManager")[:255]
        try:
            session_factory = effective_engine.resources.relational.session_factory()
            async with session_factory() as session:
                existing = await session.get(DataSource, source_config_id)
                if existing is not None:
                    return
                data_source = DataSource(
                    id=source_config_id,
                    name=name,
                    description=description,
                )
                session.add(data_source)
                try:
                    await _commit_with_sqlite_lock_retry(session, data_source)
                except IntegrityError:
                    # Another process may have provisioned the same source between
                    # our read and insert. Treat that race as success only when the
                    # parent row is now present.
                    await session.rollback()
                    if await session.get(DataSource, source_config_id) is None:
                        raise
        except SQLAlchemyError as error:
            from sag_api.core.errors import UpstreamError

            log.exception("信源父记录初始化失败 source_config_id=%s", source_config_id)
            raise UpstreamError(
                "信源引擎初始化失败，请稍后重试",
                layer=ErrorLayer.STORE,
                stage=ErrorStage.PERSIST,
            ) from error

    async def _configure_sqlite_document_store(self, engine: Any) -> None:
        """Apply bounded writer contention settings to zleap's SQLite store."""
        if self._settings.sag_relational_provider not in (None, "sqlite"):
            return
        from sqlalchemy import text

        async with engine.resources.relational.session_factory()() as session:
            await session.execute(text("PRAGMA journal_mode=WAL"))
            await session.execute(text("PRAGMA busy_timeout=30000"))

    async def _slot(self, source_config_id: str, source: Source | None = None) -> _Slot:
        slot = self._slots.get(source_config_id)
        if slot is not None and not slot.closing:
            slot.last_used = time.monotonic()
            return slot
        async with self._create_lock:
            slot = self._slots.get(source_config_id)
            if slot is None or slot.closing:
                async with self._lifecycle_gate.write():
                    log.info("构造引擎 source_config_id=%s", source_config_id)
                    config = self._config_for(source)
                    engine = DataEngine(
                        config,
                        data_source_id=source_config_id,
                        health_check=False,
                    )
                    with map_sag_errors(stage=ErrorStage.CONFIG):
                        # 0.8.2:pgvector/oceanbase 等向量后端在 start() 前必须先
                        # init_schema() 显式创建关系表与向量模式对象,否则 start()
                        # 抛 StorageInitializationRequiredError。
                        await self._ensure_engine_schema(engine)
                        await engine.start()
                        await self._configure_sqlite_document_store(engine)
                    try:
                        await self._ensure_source_config(source_config_id, source, engine)
                        await self._ensure_universe_query_indexes(engine)
                    except Exception:
                        try:
                            await engine.aclose()
                        except Exception:  # noqa: BLE001 - preserve provisioning error
                            log.exception(
                                "初始化失败后的引擎关闭异常 source_config_id=%s",
                                source_config_id,
                            )
                        raise
                    slot = _Slot(engine=engine)
                    self._slots[source_config_id] = slot
                    await self._evict_lru(keep=source_config_id)
        return slot

    async def _ensure_read_runtime(
        self,
        sources_by_config: dict[str, Source | None],
    ) -> None:
        """Initialize shared storage once without requiring one engine per read scope.

        Vector tables and the relational event graph are shared across source
        partitions. Once any live slot has initialized that runtime, read-only
        repository queries can filter by ``source_config_id`` directly. This avoids
        blocking unrelated searches behind a long-running document extraction.
        """

        if any(not slot.closing for slot in self._slots.values()):
            return
        for source_config_id in sorted(sources_by_config):
            if source_config_id.strip():
                await self._slot(
                    source_config_id,
                    sources_by_config.get(source_config_id),
                )
                return

    async def _evict_lru(self, *, keep: str) -> None:
        """超过缓存上限时逐出最久未用、且当前空闲（未持锁）的引擎槽。

        在 `_create_lock` 内调用。持锁中的槽跳过——正在服务的源不被打断。
        """
        while len(self._slots) > self._cache_size:
            candidates = [
                (s.last_used, scid)
                for scid, s in self._slots.items()
                if (scid != keep and not s.lock.locked() and s.concurrent_users == 0 and s.maintenance_requests == 0)
            ]
            if not candidates:
                break  # 其余都在忙，暂不逐出
            _, victim = min(candidates)
            slot = self._slots.pop(victim)
            slot.closing = True
            try:
                await slot.idle.wait()
                async with slot.lock:
                    await slot.engine.aclose()
                log.info("LRU 逐出引擎 source_config_id=%s（缓存上限 %d）", victim, self._cache_size)
            except Exception as e:  # noqa: BLE001
                log.warning("逐出引擎失败 %s: %s", victim, e)

    @asynccontextmanager
    async def use(self, source_config_id: str, source: Source | None = None):
        """取得该源的引擎并持有其锁（串行化本源上的操作）。"""
        while True:
            slot = await self._slot(source_config_id, source)
            async with self._lifecycle_gate.read():
                await slot.lock.acquire()
                if slot.closing:
                    slot.lock.release()
                    continue
                try:
                    slot.last_used = time.monotonic()
                    yield slot.engine
                finally:
                    slot.lock.release()
                return

    @asynccontextmanager
    async def use_concurrently(
        self,
        source_config_id: str,
        source: Source | None = None,
        *,
        should_pause: PauseCheck | None = None,
    ):
        """取得共享资源但不串行化文档处理；独立 loader/extractor 隔离可变状态。"""
        while True:
            slot = await self._slot(source_config_id, source)
            # Maintenance waiters must not hold the global lifecycle read gate;
            # otherwise a configuration reset could be delayed by work that has
            # not actually started yet.
            while not slot.concurrent_allowed.is_set():
                if should_pause is not None and await should_pause():
                    raise _DocumentAdmissionYielded
                try:
                    await asyncio.wait_for(slot.concurrent_allowed.wait(), timeout=0.1)
                except TimeoutError:
                    continue
            async with self._lifecycle_gate.read():
                async with slot.state_lock:
                    if slot.closing or not slot.concurrent_allowed.is_set():
                        continue
                    slot.concurrent_users += 1
                    slot.idle.clear()
                    slot.last_used = time.monotonic()
                try:
                    yield slot.engine
                finally:
                    async with slot.state_lock:
                        slot.concurrent_users -= 1
                        if slot.concurrent_users == 0:
                            slot.idle.set()
                return

    async def provision(self, source_config_id: str, source: Source | None = None) -> None:
        """确保该源的引擎 schema 与父记录就绪（幂等）。"""
        await self._slot(source_config_id, source)

    async def get_sag_session_factory(self, source_config_id: str, source: Source | None = None):
        """Return the relational factory owned by an initialized SAG runtime."""
        slot = await self._slot(source_config_id, source)
        return slot.engine.resources.relational.session_factory()

    @asynccontextmanager
    async def maintenance(self, source_config_id: str, source: Source | None = None):
        """Drain document processors and hold an exclusive OCTX mutation window."""
        await self.begin_document_maintenance(source_config_id, source)
        try:
            while True:
                slot = await self._slot(source_config_id, source)
                async with self._lifecycle_gate.read():
                    await slot.lock.acquire()
                    if slot.closing:
                        slot.lock.release()
                        continue
                    try:
                        yield slot.engine
                    finally:
                        slot.lock.release()
                    return
        finally:
            await self.end_document_maintenance(source_config_id, source)

    async def begin_document_maintenance(
        self,
        source_config_id: str,
        source: Source | None = None,
    ) -> None:
        """Stop admitting processors and wait until existing users drain.

        The wait deliberately happens outside the job worker pool. The caller
        owns one maintenance request until ``end_document_maintenance``.
        """
        while True:
            slot = await self._slot(source_config_id, source)
            registered = False
            try:
                async with slot.state_lock:
                    if slot.closing:
                        continue
                    slot.maintenance_requests += 1
                    slot.concurrent_allowed.clear()
                    registered = True
                await slot.idle.wait()
                if slot.closing:
                    await self.end_document_maintenance(source_config_id)
                    continue
                return
            except BaseException:
                if registered:
                    async with slot.state_lock:
                        slot.maintenance_requests = max(0, slot.maintenance_requests - 1)
                        if slot.maintenance_requests == 0:
                            slot.concurrent_allowed.set()
                raise

    async def end_document_maintenance(
        self,
        source_config_id: str,
        source: Source | None = None,
    ) -> None:
        """Release one source-maintenance request and resume admission."""
        slot = self._slots.get(source_config_id)
        if slot is None:
            return
        async with slot.state_lock:
            slot.maintenance_requests = max(0, slot.maintenance_requests - 1)
            if slot.maintenance_requests == 0 and not slot.closing:
                slot.concurrent_allowed.set()

    async def delete_document_data(
        self,
        source_config_id: str,
        document_source_id: str,
        *,
        source: Source | None = None,
    ) -> None:
        """删除一篇文档的块、事件、关系及孤立实体派生数据。"""
        from sag_api.sag.document_cleanup import delete_document_records

        while True:
            slot = await self._slot(source_config_id, source)
            async with self._lifecycle_gate.read():
                await slot.lock.acquire()
                try:
                    if slot.closing:
                        continue
                    # Searches already share ``slot.lock``. Pause admission of new
                    # concurrent document processors, then drain processors that
                    # entered before this maintenance window.
                    async with slot.state_lock:
                        slot.concurrent_allowed.clear()
                    await slot.idle.wait()
                    with map_sag_errors(stage=ErrorStage.PERSIST):
                        deleted = await delete_document_records(
                            source_config_id,
                            document_source_id,
                            session_factory=slot.engine.resources.relational.session_factory(),
                            vector_store=slot.engine.resources.vector,
                        )
                finally:
                    async with slot.state_lock:
                        if slot.maintenance_requests == 0:
                            slot.concurrent_allowed.set()
                    slot.lock.release()
                break
        log.info(
            "文档派生数据已清理 source_config_id=%s document_source_id=%s chunks=%d events=%d relations=%d entities=%d",
            source_config_id,
            document_source_id,
            len(deleted.chunk_ids),
            len(deleted.event_ids),
            len(deleted.relation_ids),
            len(deleted.entity_ids),
        )

    async def process_document(
        self,
        source_config_id: str,
        path: str | None,
        *,
        source: Source | None = None,
        on_stage: StageCallback | None = None,
        checkpoint: ProcessCheckpoint | None = None,
        on_checkpoint: CheckpointCallback | None = None,
        should_pause: PauseCheck | None = None,
        max_concurrency: int | None = None,
        document_title: str | None = None,
        original_path: str | None = None,
    ) -> ProcessOutcome:
        """独立处理一篇文档；同源文档可并行，chunk 完成即保存断点。"""

        async def ignore_checkpoint(_checkpoint: ProcessCheckpoint) -> None:
            return None

        async def never_pause() -> bool:
            return False

        effective_checkpoint = checkpoint or ProcessCheckpoint()
        effective_should_pause = should_pause or never_pause

        def paused_outcome() -> ProcessOutcome:
            return ProcessOutcome(
                source_id=effective_checkpoint.source_id,
                chunk_count=len(effective_checkpoint.chunk_ids),
                event_count=effective_checkpoint.event_count,
                chunk_ids=list(effective_checkpoint.chunk_ids),
                event_ids=list(effective_checkpoint.event_ids),
                processed_chunk_ids=list(effective_checkpoint.processed_chunk_ids),
                eventless_chunk_ids=list(effective_checkpoint.eventless_chunk_ids),
                token_usage=effective_checkpoint.token_usage,
                paused=True,
            )

        with map_sag_errors(stage=ErrorStage.EXTRACT):
            if await effective_should_pause():
                return paused_outcome()
            try:
                async with self.use_concurrently(
                    source_config_id,
                    source,
                    should_pause=effective_should_pause,
                ) as engine:
                    processor = IncrementalDocumentProcessor(
                        engine,
                        source_config_id,
                        max_concurrency=max_concurrency or self._settings.document_extract_concurrency,
                        chunk_max_tokens=self._settings.document_chunk_max_tokens,
                        chunk_mode=self._settings.document_chunk_mode,
                        document_title=document_title,
                        max_entities_per_event=(8 if self._settings.document_extraction_profile == "concise" else 20),
                        enable_strict_filtering=self._settings.document_strict_filtering,
                        event_entity_attempts=self._settings.document_event_entity_attempts,
                    )
                    if self._settings.sag_relational_provider in (None, "sqlite"):
                        async with self._sqlite_document_mutation_lock:
                            return await processor.process(
                                path,
                                checkpoint=effective_checkpoint,
                                on_checkpoint=on_checkpoint or ignore_checkpoint,
                                should_pause=effective_should_pause,
                                on_stage=on_stage,
                                original_path=original_path,
                            )
                    return await processor.process(
                        path,
                        checkpoint=effective_checkpoint,
                        on_checkpoint=on_checkpoint or ignore_checkpoint,
                        should_pause=effective_should_pause,
                        on_stage=on_stage,
                        original_path=original_path,
                    )
            except _DocumentAdmissionYielded:
                return paused_outcome()

    async def _search_raw(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None,
        strategy: str,
        top_k: int,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        return await self._search._search_raw(
            source_config_id,
            query,
            source=source,
            strategy=strategy,
            top_k=top_k,
            include_ranked_candidates=include_ranked_candidates,
        )

    async def search(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None = None,
        strategy: str | None = None,
        top_k: int | None = None,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        return await self._search.search(
            source_config_id,
            query,
            source=source,
            strategy=strategy,
            top_k=top_k,
            include_ranked_candidates=include_ranked_candidates,
        )

    async def search_many(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        strategy: str | None = None,
        top_k: int | None = None,
        exclude_source_ids_by_config: dict[str, tuple[str, ...]] | None = None,
        include_ranked_candidates: bool = False,
    ) -> SearchOutcome:
        return await self._search.search_many(
            targets,
            query,
            strategy=strategy,
            top_k=top_k,
            exclude_source_ids_by_config=exclude_source_ids_by_config,
            include_ranked_candidates=include_ranked_candidates,
        )

    async def _search_chunk_vectors(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        top_k: int,
        requested_sources: int,
        exclude_source_ids_by_config: dict[str, tuple[str, ...]] | None = None,
    ) -> SearchOutcome:
        return await self._search._search_chunk_vectors(
            targets,
            query,
            top_k=top_k,
            requested_sources=requested_sources,
            exclude_source_ids_by_config=exclude_source_ids_by_config,
        )

    async def search_event_scores(
        self,
        query: str,
        sources_by_config: dict[str, Source | None],
        *,
        limit: int | None = None,
    ) -> dict[tuple[str, str], float]:
        return await self._search.search_event_scores(query, sources_by_config, limit=limit)

    async def universe_overview_stats(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        bucket_count: int = 8,
        category_limit: int = 8,
    ) -> UniverseSourceStatsInfo:
        return await self._universe.universe_overview_stats(
            source_config_id, source=source, bucket_count=bucket_count, category_limit=category_limit
        )

    async def _universe_entity_event_counts(
        self,
        session: Any,
        source_config_id: str,
        entity_ids: list[str],
        *,
        as_of_db: datetime,
    ) -> dict[str, int]:
        return await self._universe._universe_entity_event_counts(
            session, source_config_id, entity_ids, as_of_db=as_of_db
        )

    async def _universe_event_bundles(
        self,
        session: Any,
        source_config_id: str,
        events: list[dict[str, Any]],
        *,
        as_of_db: datetime,
        entity_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return await self._universe._universe_event_bundles(
            session, source_config_id, events, as_of_db=as_of_db, entity_limit=entity_limit
        )

    async def universe_timeline(
        self,
        source_config_id: str,
        *,
        source_revision: str,
        source: Source | None = None,
        limit: int = 6,
        entity_limit: int = 8,
        direction: str = "older",
        cursor: str | None = None,
        snapshot_id: str | None = None,
    ) -> UniverseTimelineInfo:
        return await self._universe.universe_timeline(
            source_config_id,
            source_revision=source_revision,
            source=source,
            limit=limit,
            entity_limit=entity_limit,
            direction=direction,
            cursor=cursor,
            snapshot_id=snapshot_id,
        )

    async def universe_expand(
        self,
        source_config_id: str,
        node_kind: str,
        node_id: str,
        *,
        source_revision: str,
        source: Source | None = None,
        limit: int = 4,
        cursor: str | None = None,
        snapshot_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> UniverseExpansionInfo | None:
        return await self._universe.universe_expand(
            source_config_id,
            node_kind,
            node_id,
            source_revision=source_revision,
            source=source,
            limit=limit,
            cursor=cursor,
            snapshot_id=snapshot_id,
            after=after,
            before=before,
        )

    async def universe_node_detail(
        self,
        source_config_id: str,
        node_kind: str,
        node_id: str,
        *,
        source: Source | None = None,
    ) -> dict[str, Any] | None:
        return await self._universe.universe_node_detail(source_config_id, node_kind, node_id, source=source)

    async def graph_for_sections(
        self,
        sections: list[RetrievedSection],
        sources_by_config: dict[str, Source | None],
        *,
        event_limit: int = 50,
        entity_limit: int = 48,
        edge_limit: int = 96,
        event_scores: dict[tuple[str, str], float] | None = None,
    ) -> SourceGraphInfo:
        return await self._content.graph_for_sections(
            sections,
            sources_by_config,
            event_limit=event_limit,
            entity_limit=entity_limit,
            edge_limit=edge_limit,
            event_scores=event_scores,
        )

    async def list_entities(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        types: list[str] | None = None,
        limit: int = 100,
    ) -> list[EntityInfo]:
        return await self._content.list_entities(source_config_id, source=source, types=types, limit=limit)

    async def source_graph(
        self,
        source_config_id: str,
        source_ids: list[str],
        *,
        source: Source | None = None,
        event_limit: int = 1_000,
        entity_limit: int = 1_000,
        expected_event_count: int | None = None,
    ) -> SourceGraphInfo:
        return await self._content.source_graph(
            source_config_id,
            source_ids,
            source=source,
            event_limit=event_limit,
            entity_limit=entity_limit,
            expected_event_count=expected_event_count,
        )

    async def entity_context(
        self,
        source_config_id: str,
        entity_id: str,
        *,
        source: Source | None = None,
        limit: int = 20,
    ) -> list[str]:
        return await self._content.entity_context(source_config_id, entity_id, source=source, limit=limit)

    async def list_chunk_headings(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        doc_sag_id: str | None = None,
        limit: int = 300,
    ) -> list[dict]:
        return await self._content.list_chunk_headings(
            source_config_id, source=source, doc_sag_id=doc_sag_id, limit=limit
        )

    async def get_document_markdown(
        self,
        source_config_id: str,
        article_id: str,
        *,
        source: Source | None = None,
    ) -> str | None:
        return await self._content.get_document_markdown(source_config_id, article_id, source=source)

    async def grep_chunks(
        self,
        source_config_id: str,
        pattern: str,
        *,
        source: Source | None = None,
        limit: int = 20,
        exclude_source_ids: tuple[str, ...] = (),
    ) -> list[dict]:
        return await self._content.grep_chunks(
            source_config_id, pattern, source=source, limit=limit, exclude_source_ids=exclude_source_ids
        )

    async def get_chunk(
        self,
        source_config_id: str,
        chunk_id: str,
        *,
        source: Source | None = None,
    ):
        return await self._content.get_chunk(source_config_id, chunk_id, source=source)

    async def release(self, source_config_id: str) -> None:
        """关闭并移除某源的引擎槽（信源删除时调用；幂等）。"""
        async with self._create_lock:
            async with self._lifecycle_gate.write():
                slot = self._slots.pop(source_config_id, None)
                if slot is None:
                    return
                slot.closing = True
                try:
                    await slot.idle.wait()
                    async with slot.lock:  # 等待在途操作结束
                        await slot.engine.aclose()
                except Exception as e:  # noqa: BLE001
                    log.warning("释放引擎失败 %s: %s", source_config_id, e)

    async def aclose_all(self) -> None:
        # 先标记并摘除，阻止新请求拿到即将关闭的槽；逐槽等待在途操作完成。
        async with self._create_lock:
            async with self._lifecycle_gate.write():
                slots = list(self._slots.items())
                for _, slot in slots:
                    slot.closing = True
                self._slots.clear()
                for scid, slot in slots:
                    try:
                        await slot.idle.wait()
                        async with slot.lock:
                            await slot.engine.aclose()
                    except Exception as e:  # noqa: BLE001
                        log.warning("关闭引擎失败 %s: %s", scid, e)
