"""EngineManager —— 管理 zleap-sag `DataEngine` 的生命周期与调用。

每个信源（source_config_id）对应一个 `DataEngine` 实例（引擎「一实例一源」的语义）。
引擎按需构造并缓存；每源一把锁串行化该源上的读写。生命周期读写闸门允许
已构造引擎跨源并发；文档处理使用独立 loader/extractor 同源并发。创建、逐出或
关闭引擎时会等待所有在途操作结束。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zleap.sag import DataEngine

from sag_api.core.config import Settings
from sag_api.core.logging import get_logger
from sag_api.enums import SEARCH_STRATEGIES, normalize_search_strategy
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.dto import (
    EntityInfo,
    GraphAssociationInfo,
    GraphEventInfo,
    ProcessCheckpoint,
    ProcessOutcome,
    RetrievedSection,
    SearchOutcome,
    SourceGraphInfo,
    UniverseExpansionInfo,
    UniverseSourceStatsInfo,
    UniverseTimeBucketInfo,
    UniverseTimelineBundleInfo,
    UniverseTimelineInfo,
)
from sag_api.sag.errors import map_sag_errors
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

if TYPE_CHECKING:
    from sag_api.db.models import Source

log = get_logger("sag")

StageCallback = Callable[[str], Awaitable[None]]
CheckpointCallback = Callable[[ProcessCheckpoint], Awaitable[None]]
PauseCheck = Callable[[], Awaitable[bool]]


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}".encode("ascii"))


def _universe_cursor_scope(source_config_id: str) -> str:
    return hashlib.sha256(source_config_id.encode("utf-8")).hexdigest()[:24]


def _encode_universe_cursor(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_urlsafe_encode(raw)}.{_urlsafe_encode(signature)}"


def _decode_universe_cursor(value: str, secret: str) -> dict[str, Any]:
    if not value or len(value) > 2048:
        raise ValueError("invalid universe cursor")
    try:
        encoded_payload, encoded_signature = value.split(".", 1)
        decoded = _urlsafe_decode(encoded_payload)
        signature = _urlsafe_decode(encoded_signature)
        expected = hmac.new(secret.encode("utf-8"), decoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid universe cursor")
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise ValueError("invalid universe cursor") from error
    if not isinstance(payload, dict) or payload.get("v") != 2:
        raise ValueError("invalid universe cursor")
    return payload


def _cursor_float(payload: dict[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid universe cursor") from error
    if not math.isfinite(value):
        raise ValueError("invalid universe cursor")
    return value


def _cursor_datetime(payload: dict[str, Any], key: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(payload[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid universe cursor") from error
    return value


def _cursor_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    # bool is an int subclass; a "true" rank is a malformed cursor, not rank 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid universe cursor")
    return value


def _weight(value: Any) -> float:
    return float(value) if value is not None else 1.0


def _database_time(value: datetime | None) -> datetime | None:
    """zleap-sag currently stores naive UTC datetimes in SQLite."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_time(value: datetime | None) -> datetime | None:
    """Normalize zleap-sag's naive SQLite timestamps at the API boundary."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass
class _Slot:
    engine: DataEngine
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    concurrent_allowed: asyncio.Event = field(default_factory=asyncio.Event)
    concurrent_users: int = 0
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
    def __init__(self, settings: Settings):
        self._settings = settings
        self._slots: dict[str, _Slot] = {}
        self._create_lock = asyncio.Lock()
        self._lifecycle_gate = _EngineLifecycleGate()
        self._cache_size = max(1, settings.engine_cache_size)
        self._universe_indexes_ready = False

    async def _ensure_universe_query_indexes(self) -> None:
        """Best-effort composite indexes for bounded timeline and neighbor reads."""
        if self._universe_indexes_ready:
            return
        self._universe_indexes_ready = True
        try:
            from sqlalchemy import Index, func
            from zleap.sag.db import get_engine
            from zleap.sag.db.models import EventEntity, SourceEvent

            specs = (
                (
                    "idx_universe_event_timeline",
                    SourceEvent.source_config_id,
                    SourceEvent.start_time,
                    SourceEvent.created_time,
                    SourceEvent.id,
                ),
                (
                    "idx_universe_event_category_timeline",
                    SourceEvent.source_config_id,
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
                    source = quote(SourceEvent.source_config_id.name)
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
                        SourceEvent.source_config_id,
                        event_time,
                        SourceEvent.id,
                    ).create(sync_connection, checkfirst=True)
                    Index(
                        "idx_universe_event_category_effective_timeline",
                        SourceEvent.source_config_id,
                        SourceEvent.category,
                        event_time,
                        SourceEvent.id,
                    ).create(sync_connection, checkfirst=True)

            async with get_engine().begin() as connection:
                await connection.run_sync(create_missing)
        except Exception:  # noqa: BLE001 - indexes are an optimization, never availability
            log.exception("创建知识宇宙查询索引失败，继续使用现有索引")

    def _effective_search_strategy(self, requested: str | None) -> str:
        """只允许快速/精确两档，并兼容存量内部配置中的 atomic。"""
        raw = requested or self._settings.search_strategy
        strategy = normalize_search_strategy(raw)
        if strategy in SEARCH_STRATEGIES:
            if strategy != raw:
                log.info("旧检索策略 %s 已按精确模式 multi 执行", raw)
            return strategy
        fallback = normalize_search_strategy(self._settings.search_strategy)
        if fallback not in SEARCH_STRATEGIES:
            fallback = "vector"
        log.warning("忽略不支持的检索策略 %s，改用 %s", raw, fallback)
        return fallback

    def _config_for(self, source: Source | None) -> Any:
        overrides = None
        if source is not None and source.config:
            overrides = source.config.get("engine")
        return build_engine_config(self._settings, overrides=overrides)

    async def _ensure_source_config(
        self,
        source_config_id: str,
        source: Source | None = None,
    ) -> None:
        """Ensure the zleap-sag parent row exists before derived data is written.

        ``DataEngine.start()`` initializes storage schemas but does not create a
        ``SourceConfig`` row. The upstream ``ingest()`` convenience method normally
        creates it; our incremental processor uses the lower-level loader directly,
        so the adapter must preserve that invariant itself.
        """
        from sqlalchemy.exc import IntegrityError, SQLAlchemyError
        from zleap.sag.db import SourceConfig, get_session_factory

        name = str(getattr(source, "name", "") or f"sag-{source_config_id[-8:]}")[:100]
        description = str(getattr(source, "description", "") or "created by sag EngineManager")[:255]
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                if await session.get(SourceConfig, source_config_id) is not None:
                    return
                session.add(
                    SourceConfig(
                        id=source_config_id,
                        name=name,
                        description=description,
                        target_config={},
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    # Another process may have provisioned the same source between
                    # our read and insert. Treat that race as success only when the
                    # parent row is now present.
                    await session.rollback()
                    if await session.get(SourceConfig, source_config_id) is None:
                        raise
        except SQLAlchemyError as error:
            from sag_api.core.errors import UpstreamError

            log.exception("信源父记录初始化失败 source_config_id=%s", source_config_id)
            raise UpstreamError("信源引擎初始化失败，请稍后重试") from error

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
                        source_config_id=source_config_id,
                        health_check=False,
                    )
                    with map_sag_errors():
                        await engine.start()
                    try:
                        await self._ensure_source_config(source_config_id, source)
                        await self._ensure_universe_query_indexes()
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
                if scid != keep and not s.lock.locked() and s.concurrent_users == 0
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
    async def use_concurrently(self, source_config_id: str, source: Source | None = None):
        """取得共享资源但不串行化文档处理；独立 loader/extractor 隔离可变状态。"""
        while True:
            slot = await self._slot(source_config_id, source)
            # Maintenance waiters must not hold the global lifecycle read gate;
            # otherwise a configuration reset could be delayed by work that has
            # not actually started yet.
            await slot.concurrent_allowed.wait()
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
                    with map_sag_errors():
                        deleted = await delete_document_records(
                            source_config_id,
                            document_source_id,
                        )
                finally:
                    async with slot.state_lock:
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
    ) -> ProcessOutcome:
        """独立处理一篇文档；同源文档可并行，chunk 完成即保存断点。"""

        async def ignore_checkpoint(_checkpoint: ProcessCheckpoint) -> None:
            return None

        async def never_pause() -> bool:
            return False

        with map_sag_errors():
            async with self.use_concurrently(source_config_id, source) as engine:
                processor = IncrementalDocumentProcessor(
                    engine,
                    source_config_id,
                    max_concurrency=max_concurrency or self._settings.document_extract_concurrency,
                    chunk_max_tokens=self._settings.document_chunk_max_tokens,
                    chunk_mode=self._settings.document_chunk_mode,
                    document_title=document_title,
                    enable_strict_filtering=self._settings.document_strict_filtering,
                )
                return await processor.process(
                    path,
                    checkpoint=checkpoint or ProcessCheckpoint(),
                    on_checkpoint=on_checkpoint or ignore_checkpoint,
                    should_pause=should_pause or never_pause,
                    on_stage=on_stage,
                )

    async def _search_raw(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None,
        strategy: str,
        top_k: int,
    ) -> SearchOutcome:
        """单次检索（带每源时限）。超时抛 asyncio.TimeoutError。"""
        timeout = max(1.0, self._settings.search_source_timeout)

        async def run() -> Any:
            # The timeout must include waiting for the per-source lock. Otherwise
            # concurrent searches can queue forever before the timed region starts.
            with map_sag_errors():
                async with self.use(source_config_id, source) as engine:
                    return await engine.search(query, strategy=strategy, top_k=top_k)

        result = await asyncio.wait_for(run(), timeout)
        return SearchOutcome.from_result(result)

    async def search(
        self,
        source_config_id: str,
        query: str,
        *,
        source: Source | None = None,
        strategy: str | None = None,
        top_k: int | None = None,
    ) -> SearchOutcome:
        """检索（韧性版）：精确模式超时/失败/空结果时回退快速模式。

        精确模式的查询侧含 LLM 实体抽取（慢且可能失败重试）；事件向量层缺失的源也会空转。
        回退把这类退化收敛为一次快速向量检索，可经 `search_fallback_vector=false` 关闭。
        """
        strategy = self._effective_search_strategy(strategy)
        top_k = top_k or self._settings.search_top_k
        try:
            outcome = await self._search_raw(source_config_id, query, source=source, strategy=strategy, top_k=top_k)
            if outcome.sections or strategy == "vector" or not self._settings.search_fallback_vector:
                return outcome
            log.info("精确检索空结果，回退快速检索 source_config_id=%s", source_config_id)
        except TimeoutError:
            if strategy == "vector" or not self._settings.search_fallback_vector:
                raise
            log.warning(
                "检索超时(%.0fs) 回退 vector source_config_id=%s strategy=%s",
                self._settings.search_source_timeout,
                source_config_id,
                strategy,
            )
        except Exception as e:  # noqa: BLE001
            if strategy == "vector" or not self._settings.search_fallback_vector:
                raise
            log.warning(
                "检索失败回退 vector source_config_id=%s strategy=%s err=%s",
                source_config_id,
                strategy,
                getattr(e, "message", None) or e,
            )
        return await self._search_raw(source_config_id, query, source=source, strategy="vector", top_k=top_k)

    async def search_many(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        strategy: str | None = None,
        top_k: int | None = None,
    ) -> SearchOutcome:
        """在统一候选与并发边界内检索；单源失败不影响整体结果。"""
        strategy = self._effective_search_strategy(strategy)
        top_k = top_k or self._settings.search_top_k
        per_source_k = max(top_k, 4)
        requested_sources = len(targets)
        targets = targets[: self._settings.search_source_candidate_limit]

        if strategy == "vector" and targets:
            try:
                return await self._search_chunk_vectors(
                    targets,
                    query,
                    top_k=top_k,
                    requested_sources=requested_sources,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # The lexical branch in ``retrieve_relevant_sections`` is already
                # running in parallel. Return control to it instead of paying a
                # second timeout in the legacy per-source vector path.
                log.warning("批量块向量召回超时，保留并行词法结果")
                return SearchOutcome(
                    query=query,
                    sections=[],
                    stats={
                        "sources": len(targets),
                        "sources_requested": requested_sources,
                        "source_limit_applied": requested_sources > len(targets),
                        "candidates": 0,
                        "chunk_recall": "batch-vector-timeout",
                    },
                )
            except Exception as error:  # noqa: BLE001
                # Keep the established per-source engine path as a compatibility
                # fallback for storage providers that cannot do a filtered batch kNN.
                log.warning("批量块向量召回失败，回退逐信源检索：%s", error)

        semaphore = asyncio.Semaphore(self._settings.search_source_concurrency)

        async def _one(scid: str, source: Source | None):
            async with semaphore:
                try:
                    outcome = await self.search(scid, query, source=source, strategy=strategy, top_k=per_source_k)
                    return scid, outcome
                except Exception as e:  # noqa: BLE001
                    log.warning("fan-out 检索失败 %s：%s", scid, getattr(e, "message", None) or e)
                    return None

        results = await asyncio.gather(*(_one(scid, src) for scid, src in targets))

        best: dict[tuple[str, str], RetrievedSection] = {}
        loose: list[RetrievedSection] = []
        for result in results:
            if result is None:
                continue
            scid, outcome = result
            for sec in outcome.sections:
                # 部分向量后端不会回填来源；跨源聚合时补齐，供 MCP/UI 正确标注。
                if not sec.source_config_id:
                    sec.source_config_id = scid
                if sec.chunk_id:
                    key = (sec.source_config_id, sec.chunk_id)
                    prev = best.get(key)
                    if prev is None or sec.score > prev.score:
                        best[key] = sec
                else:
                    loose.append(sec)
        merged = sorted([*best.values(), *loose], key=lambda x: x.score, reverse=True)[:top_k]
        return SearchOutcome(
            query=query,
            sections=merged,
            stats={
                "sources": len(targets),
                "sources_requested": requested_sources,
                "source_limit_applied": requested_sources > len(targets),
                "candidates": len(best) + len(loose),
            },
        )

    async def _search_chunk_vectors(
        self,
        targets: list[tuple[str, Source | None]],
        query: str,
        *,
        top_k: int,
        requested_sources: int,
    ) -> SearchOutcome:
        """Recall chunks across all selected sources with one query embedding."""

        sources_by_config = {source_config_id: source for source_config_id, source in targets}
        source_config_ids = list(sources_by_config)
        await self._ensure_read_runtime(sources_by_config)

        from zleap.sag.core.storage.client import get_es_client
        from zleap.sag.core.storage.repositories.source_chunk_repository import (
            SourceChunkRepository,
        )
        from zleap.sag.modules.load.processor import DocumentProcessor

        async def recall() -> list[dict[str, Any]]:
            query_vector = await DocumentProcessor().generate_embedding(query)
            repository = SourceChunkRepository(get_es_client())
            return await repository.search_similar_by_content(
                query_vector=query_vector,
                k=top_k,
                source_config_ids=source_config_ids,
            )

        hits = await asyncio.wait_for(
            recall(),
            timeout=max(1.0, self._settings.search_source_timeout),
        )
        allowed_sources = set(source_config_ids)
        sections: dict[tuple[str, str], RetrievedSection] = {}
        loose: list[RetrievedSection] = []
        for hit in hits:
            source_config_id = str(hit.get("source_config_id") or "").strip()
            if source_config_id not in allowed_sources:
                continue
            try:
                score = float(hit.get("_score") or hit.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(score):
                continue
            section = RetrievedSection(
                chunk_id=str(hit.get("chunk_id") or "").strip() or None,
                heading=str(hit.get("heading") or "").strip(),
                content=str(hit.get("content") or "").strip(),
                score=max(0.0, min(1.0, score)),
                rank=int(hit.get("rank") or 0),
                source_id=str(hit.get("source_id") or "").strip() or None,
                source_config_id=source_config_id,
            )
            if not section.chunk_id:
                loose.append(section)
                continue
            key = (source_config_id, section.chunk_id)
            previous = sections.get(key)
            if previous is None or section.score > previous.score:
                sections[key] = section

        merged = sorted(
            [*sections.values(), *loose],
            key=lambda section: (-section.score, section.source_config_id or "", section.chunk_id or ""),
        )[:top_k]
        return SearchOutcome(
            query=query,
            sections=merged,
            stats={
                "sources": len(targets),
                "sources_requested": requested_sources,
                "source_limit_applied": requested_sources > len(targets),
                "candidates": len(sections) + len(loose),
                "chunk_recall": "batch-vector",
            },
        )

    async def search_event_scores(
        self,
        query: str,
        sources_by_config: dict[str, Source | None],
        *,
        limit: int | None = None,
    ) -> dict[tuple[str, str], float]:
        """Recall extracted events directly from their title/content vectors.

        Chunk retrieval remains the evidence path used by answers and citations. This
        independent event path prevents long documents with sparse event-bearing
        chunks from degrading into chunk-only search results.
        """

        query = query.strip()
        source_config_ids = sorted(
            source_config_id.strip() for source_config_id in sources_by_config if source_config_id.strip()
        )
        if not query or not source_config_ids:
            return {}

        bounded_limit = max(
            1,
            min(int(limit or self._settings.search_top_k), 50),
        )
        candidate_limit = min(200, max(bounded_limit * 4, 24))

        await self._ensure_read_runtime(sources_by_config)

        from zleap.sag.core.storage.client import get_es_client
        from zleap.sag.core.storage.repositories.event_repository import (
            EventVectorRepository,
        )
        from zleap.sag.modules.load.processor import DocumentProcessor

        async def recall_vectors() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            query_vector = await DocumentProcessor().generate_embedding(query)
            repository = EventVectorRepository(get_es_client())
            return await asyncio.gather(
                repository.search_similar_by_title(
                    query_vector=query_vector,
                    k=candidate_limit,
                    source_config_ids=source_config_ids,
                ),
                repository.search_similar_by_content(
                    query_vector=query_vector,
                    k=candidate_limit,
                    source_config_ids=source_config_ids,
                ),
            )

        title_hits, content_hits = await asyncio.wait_for(
            recall_vectors(),
            timeout=max(1.0, self._settings.search_source_timeout),
        )

        allowed_sources = set(source_config_ids)
        scores: dict[tuple[str, str], float] = {}
        channels: dict[tuple[str, str], int] = {}
        for channel, weight, hits in (
            (1, 1.0, title_hits),
            (2, 0.95, content_hits),
        ):
            for hit in hits:
                event_id = str(hit.get("event_id") or hit.get("id") or "").strip()
                source_config_id = str(hit.get("source_config_id") or "").strip()
                if not event_id or source_config_id not in allowed_sources:
                    continue
                try:
                    score_value = hit.get("_score")
                    if score_value is None:
                        score_value = hit.get("score", 0.0)
                    raw_score = float(score_value or 0.0)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(raw_score):
                    continue
                key = (source_config_id, event_id)
                score = max(0.0, min(1.0, raw_score)) * weight
                scores[key] = max(scores.get(key, 0.0), score)
                channels[key] = channels.get(key, 0) | channel

        if not scores:
            return {}

        # Agreement between title and content vectors is a small confidence signal,
        # while the raw cosine score remains the dominant ordering factor.
        for key, channel_mask in channels.items():
            if channel_mask == 3:
                scores[key] = min(1.0, scores[key] + 0.03)

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        strongest = ranked[0][1]
        relevance_floor = max(0.20, strongest * 0.55)
        return {key: round(score, 6) for key, score in ranked[:bounded_limit] if score >= relevance_floor}

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
        """Build a graph from direct event hits plus events attached to evidence chunks."""
        bounded_event_limit = max(1, min(int(event_limit), 100))
        bounded_entity_limit = max(1, min(int(entity_limit), 100))
        bounded_edge_limit = max(1, min(int(edge_limit), 300))
        direct_event_scores: dict[tuple[str, str], float] = {}
        for (raw_source_config_id, raw_event_id), raw_score in (event_scores or {}).items():
            source_config_id = raw_source_config_id.strip()
            event_id = raw_event_id.strip()
            if not source_config_id or not event_id:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score):
                direct_event_scores[(source_config_id, event_id)] = max(0.0, min(1.0, score))
        direct_event_ids_by_config: dict[str, set[str]] = {}
        for source_config_id, event_id in direct_event_scores:
            direct_event_ids_by_config.setdefault(source_config_id, set()).add(event_id)
        chunk_scores: dict[tuple[str, str], float] = {}
        chunk_ids_by_config: dict[str, set[str]] = {}
        for section in sections:
            source_config_id = (section.source_config_id or "").strip()
            chunk_id = (section.chunk_id or "").strip()
            if not source_config_id or not chunk_id:
                continue
            key = (source_config_id, chunk_id)
            chunk_scores[key] = max(chunk_scores.get(key, 0.0), section.score)
            chunk_ids_by_config.setdefault(source_config_id, set()).add(chunk_id)

        requested_config_ids = set(chunk_ids_by_config) | set(direct_event_ids_by_config)
        if not requested_config_ids:
            return SourceGraphInfo()

        await self._ensure_read_runtime(
            {source_config_id: sources_by_config.get(source_config_id) for source_config_id in requested_config_ids}
        )

        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        section_filters = [
            and_(
                SourceEvent.source_config_id == source_config_id,
                SourceEvent.chunk_id.in_(chunk_ids),
            )
            for source_config_id, chunk_ids in chunk_ids_by_config.items()
        ]
        event_filters = [
            and_(
                SourceEvent.source_config_id == source_config_id,
                SourceEvent.id.in_(event_ids),
            )
            for source_config_id, event_ids in direct_event_ids_by_config.items()
        ]
        candidate_filters = [*section_filters, *event_filters]
        candidate_limit = min(
            200,
            max(
                bounded_event_limit * 4,
                bounded_event_limit,
                len(direct_event_scores) * 2,
                len(chunk_scores) * 8 + len(direct_event_scores),
            ),
        )
        per_chunk_limit = max(
            1,
            min(8, math.ceil(candidate_limit / max(1, len(chunk_scores)))),
        )
        chunk_rank = (
            func.row_number()
            .over(
                partition_by=(SourceEvent.source_config_id, SourceEvent.chunk_id),
                order_by=(SourceEvent.rank.asc(), SourceEvent.id.asc()),
            )
            .label("chunk_rank")
        )
        ranked_events = (
            select(
                SourceEvent.id.label("id"),
                SourceEvent.source_config_id.label("source_config_id"),
                SourceEvent.source_id.label("source_id"),
                SourceEvent.title.label("title"),
                SourceEvent.summary.label("summary"),
                SourceEvent.content.label("content"),
                SourceEvent.category.label("category"),
                SourceEvent.rank.label("rank"),
                SourceEvent.parent_id.label("parent_id"),
                SourceEvent.chunk_id.label("chunk_id"),
                SourceEvent.start_time.label("start_time"),
                chunk_rank,
            )
            .where(
                or_(*candidate_filters),
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
            )
            .subquery()
        )
        direct_event_ids = {event_id for _source_config_id, event_id in direct_event_scores}
        rank_condition = ranked_events.c.chunk_rank <= per_chunk_limit
        if direct_event_ids:
            rank_condition = or_(
                rank_condition,
                ranked_events.c.id.in_(direct_event_ids),
            )
        sf = get_session_factory()
        async with sf() as session:
            event_rows = (
                (
                    await session.execute(
                        select(ranked_events)
                        .where(rank_condition)
                        .order_by(
                            ranked_events.c.chunk_rank.asc(),
                            ranked_events.c.rank.asc(),
                            ranked_events.c.id.asc(),
                        )
                        .limit(candidate_limit)
                    )
                )
                .mappings()
                .all()
            )
            events_by_chunk: dict[tuple[str, str], list[Any]] = {}
            for row in event_rows:
                key = (row["source_config_id"], row["chunk_id"] or "")
                events_by_chunk.setdefault(key, []).append(row)
            for rows in events_by_chunk.values():
                rows.sort(key=lambda row: (int(row["rank"] or 0), row["id"]))

            direct_rows = sorted(
                (row for row in event_rows if (row["source_config_id"], row["id"]) in direct_event_scores),
                key=lambda row: (
                    -direct_event_scores[(row["source_config_id"], row["id"])],
                    int(row["rank"] or 0),
                    row["id"],
                ),
            )
            selected_direct_keys = {(row["source_config_id"], row["id"]) for row in direct_rows}

            # 每个高相关分块先贡献一个事件，再进入下一轮，避免单个长分块占满结果。
            ordered_chunk_keys = sorted(
                chunk_scores,
                key=lambda key: (-chunk_scores[key], key[0], key[1]),
            )
            balanced_rows: list[Any] = []
            depth = 0
            while True:
                added = False
                for key in ordered_chunk_keys:
                    rows = events_by_chunk.get(key, [])
                    if depth >= len(rows):
                        continue
                    balanced_rows.append(rows[depth])
                    added = True
                if not added:
                    break
                depth += 1

            # 重复上传或重叠分块会抽取出同名事件；同一信源只保留相关度最高的一条。
            seen_titles: set[tuple[str, str]] = set()
            event_rows = []
            for row in [
                *direct_rows,
                *(row for row in balanced_rows if (row["source_config_id"], row["id"]) not in selected_direct_keys),
            ]:
                normalized_title = re.sub(r"\s+", "", str(row["title"] or "")).casefold()
                title_key = (row["source_config_id"], normalized_title or row["id"])
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                event_rows.append(row)
                if len(event_rows) >= bounded_event_limit:
                    break
            events = [
                GraphEventInfo(
                    id=row["id"],
                    source_config_id=row["source_config_id"],
                    source_id=row["source_id"],
                    title=row["title"] or "未命名事件",
                    summary=str(row["summary"] or "")[:800],
                    content=str(row["content"] or "")[:4000],
                    category=row["category"] or "",
                    rank=int(row["rank"] or 0),
                    parent_id=row["parent_id"],
                    chunk_id=row["chunk_id"],
                    start_time=row["start_time"],
                    score=direct_event_scores.get(
                        (row["source_config_id"], row["id"]),
                        chunk_scores.get(
                            (row["source_config_id"], row["chunk_id"] or ""),
                            0.0,
                        ),
                    ),
                )
                for row in event_rows
            ]
            event_ids = [event.id for event in events]
            if not event_ids:
                return SourceGraphInfo(events=events)

            association_limit = min(
                bounded_edge_limit,
                max(bounded_event_limit * 4, bounded_entity_limit * 3),
            )
            association_rows = (
                await session.execute(
                    select(
                        EventEntity.event_id,
                        EventEntity.entity_id,
                        EventEntity.weight,
                        EventEntity.description,
                        Entity.name,
                        Entity.type,
                        Entity.description.label("entity_description"),
                    )
                    .join(Entity, Entity.id == EventEntity.entity_id)
                    .where(
                        EventEntity.event_id.in_(event_ids),
                        Entity.source_config_id.in_(requested_config_ids),
                    )
                    .order_by(EventEntity.weight.desc(), EventEntity.created_time.asc())
                    .limit(association_limit)
                )
            ).all()

        heat: dict[str, int] = {}
        entity_rows: dict[str, tuple[str, str, str]] = {}
        for _event_id, entity_id, _weight, _description, name, kind, description in association_rows:
            heat[entity_id] = heat.get(entity_id, 0) + 1
            entity_rows[entity_id] = (
                str(name or "")[:500],
                str(kind or "")[:50],
                str(description or "")[:500],
            )
        selected_entity_ids = {
            entity_id
            for entity_id, _count in sorted(
                heat.items(),
                key=lambda item: (-item[1], entity_rows[item[0]][0], item[0]),
            )[:bounded_entity_limit]
        }
        entities = [
            EntityInfo(
                id=entity_id,
                name=entity_rows[entity_id][0],
                type=entity_rows[entity_id][1],
                description=entity_rows[entity_id][2],
                heat=heat[entity_id],
            )
            for entity_id in selected_entity_ids
        ]
        entities.sort(key=lambda entity: (-entity.heat, entity.name, entity.id))
        associations = [
            GraphAssociationInfo(
                event_id=event_id,
                entity_id=entity_id,
                weight=float(weight or 1.0),
                description=str(description or "")[:240],
            )
            for event_id, entity_id, weight, description, *_rest in association_rows
            if entity_id in selected_entity_ids
        ][:bounded_edge_limit]
        return SourceGraphInfo(
            events=events,
            entities=entities,
            associations=associations,
            total_entities=len(entities),
        )

    async def list_entities(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        types: list[str] | None = None,
        limit: int = 100,
    ) -> list[EntityInfo]:
        """读取该源的事件—实体图谱，按热度（关联事件数）排序。extract 后才有数据。"""
        await self._slot(source_config_id, source)  # 确保引擎 / DB 已初始化
        from sqlalchemy import func, select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, EventEntity

        heat = func.count(EventEntity.id)
        conds = [Entity.source_config_id == source_config_id]
        if types:
            conds.append(Entity.type.in_(list(types)))
        stmt = (
            select(Entity, heat.label("heat"))
            .outerjoin(EventEntity, EventEntity.entity_id == Entity.id)
            .where(*conds)
            .group_by(Entity.id)
            .order_by(heat.desc())
            .limit(limit)
        )
        sf = get_session_factory()
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        return [
            EntityInfo(
                id=e.id,
                name=e.name or "",
                type=e.type or "",
                description=e.description or "",
                heat=int(h or 0),
            )
            for e, h in rows
        ]

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
        """按展示预算读取一个按文档均衡的事件—实体图谱。

        图谱只读取本次展示文档对应的引擎 source_id。事件使用窗口排名轮询各文档，
        避免单篇长文占满配额；关联边覆盖优先并限制密度，避免高基数图谱拖垮浏览器。
        """
        await self._slot(source_config_id, source)
        from sqlalchemy import func, select, update
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        sf = get_session_factory()
        async with sf() as s:
            if not source_ids:
                return SourceGraphInfo()

            event_scope = (
                SourceEvent.source_config_id == source_config_id,
                SourceEvent.source_id.in_(source_ids),
            )
            visible_event = SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")

            # 旧版断点抽取逐块保存，上游却把每次保存都视为整篇替换，导致先前块被隐藏。
            # 只有 Web 完成数能证明引擎中的全部事件属于本次抽取时才修复，避免恢复旧版本。
            if expected_event_count and expected_event_count > 0:
                total_event_count, visible_event_count = (
                    await s.execute(
                        select(
                            func.count(SourceEvent.id),
                            func.count(SourceEvent.id).filter(visible_event),
                        ).where(*event_scope)
                    )
                ).one()
                if (
                    int(visible_event_count or 0) < expected_event_count
                    and int(total_event_count or 0) == expected_event_count
                ):
                    repaired = await s.execute(
                        update(SourceEvent)
                        .where(*event_scope, SourceEvent.status == "DELETED")
                        .values(status="COMPLETED")
                    )
                    await s.commit()
                    log.warning(
                        "已恢复分块抽取中被上游隐藏的事件 source_config_id=%s count=%d",
                        source_config_id,
                        int(repaired.rowcount or 0),
                    )

            # 实体总量必须与当前文档范围一致；否则单文档筛选会把整个信源的
            # 实体数当成分母，错误显示为“已截断”。
            total_entities = int(
                (
                    await s.execute(
                        select(func.count(func.distinct(EventEntity.entity_id)))
                        .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                        .where(*event_scope, visible_event)
                    )
                ).scalar_one()
                or 0
            )

            # 每个文档先取 rank 较小的事件，再在文档之间轮询，兼顾层级根节点与覆盖面。
            source_rank = (
                func.row_number()
                .over(
                    partition_by=SourceEvent.source_id,
                    order_by=(SourceEvent.rank.asc(), SourceEvent.created_time.desc()),
                )
                .label("source_rank")
            )
            ranked = (
                select(
                    SourceEvent.id.label("id"),
                    SourceEvent.source_id.label("source_id"),
                    SourceEvent.title.label("title"),
                    SourceEvent.summary.label("summary"),
                    SourceEvent.category.label("category"),
                    SourceEvent.rank.label("rank"),
                    SourceEvent.parent_id.label("parent_id"),
                    SourceEvent.chunk_id.label("chunk_id"),
                    SourceEvent.start_time.label("start_time"),
                    SourceEvent.created_time.label("created_time"),
                    source_rank,
                )
                .where(
                    *event_scope,
                    visible_event,
                )
                .subquery()
            )
            event_rows = (
                (
                    await s.execute(
                        select(ranked)
                        .order_by(ranked.c.source_rank.asc(), ranked.c.created_time.desc())
                        .limit(event_limit)
                    )
                )
                .mappings()
                .all()
            )
            events = [
                GraphEventInfo(
                    id=row["id"],
                    source_config_id=source_config_id,
                    source_id=row["source_id"],
                    title=row["title"] or "未命名事件",
                    summary=str(row["summary"] or "")[:800],
                    category=row["category"] or "",
                    rank=int(row["rank"] or 0),
                    parent_id=row["parent_id"],
                    chunk_id=row["chunk_id"],
                    start_time=row["start_time"],
                )
                for row in event_rows
            ]
            event_ids = [event.id for event in events]
            if not event_ids:
                return SourceGraphInfo(events=events, total_entities=total_entities)

            relation_limit = min(12_000, max(300, event_limit * 2, entity_limit * 2))
            association_limit = min(24_000, relation_limit * 4)
            association_rows = (
                await s.execute(
                    select(
                        EventEntity.event_id,
                        EventEntity.entity_id,
                        EventEntity.weight,
                        EventEntity.description,
                        Entity.name,
                        Entity.type,
                        Entity.description.label("entity_description"),
                    )
                    .join(Entity, Entity.id == EventEntity.entity_id)
                    .where(
                        EventEntity.event_id.in_(event_ids),
                        Entity.source_config_id == source_config_id,
                    )
                    .order_by(EventEntity.weight.desc(), EventEntity.created_time.asc())
                    .limit(association_limit)
                )
            ).all()

        # 热度在当前图谱切片中计算；优先保留跨事件出现的实体。
        heat: dict[str, int] = {}
        entity_rows: dict[str, tuple[str, str, str]] = {}
        for _event_id, entity_id, _weight, _description, name, kind, entity_description in association_rows:
            heat[entity_id] = heat.get(entity_id, 0) + 1
            entity_rows[entity_id] = (
                str(name or "")[:500],
                str(kind or "")[:50],
                str(entity_description or "")[:500],
            )
        selected_entity_ids = {
            entity_id
            for entity_id, _count in sorted(
                heat.items(),
                key=lambda item: (-item[1], entity_rows[item[0]][0], item[0]),
            )[:entity_limit]
        }
        entities = [
            EntityInfo(
                id=entity_id,
                name=entity_rows[entity_id][0],
                type=entity_rows[entity_id][1],
                description=entity_rows[entity_id][2],
                heat=heat[entity_id],
            )
            for entity_id in selected_entity_ids
        ]
        entities.sort(key=lambda entity: (-entity.heat, entity.name, entity.id))

        eligible_rows = [row for row in association_rows if row[1] in selected_entity_ids]
        covering_rows = []
        remaining_rows = []
        covered_events: set[str] = set()
        covered_entities: set[str] = set()
        for row in eligible_rows:
            event_id, entity_id = row[0], row[1]
            if event_id not in covered_events or entity_id not in covered_entities:
                covering_rows.append(row)
                covered_events.add(event_id)
                covered_entities.add(entity_id)
            else:
                remaining_rows.append(row)
        selected_rows = [*covering_rows, *remaining_rows][:relation_limit]

        associations = [
            GraphAssociationInfo(
                event_id=event_id,
                entity_id=entity_id,
                weight=float(weight or 1.0),
                description=str(description or "")[:240],
            )
            for event_id, entity_id, weight, description, *_rest in selected_rows
        ]
        return SourceGraphInfo(
            events=events,
            entities=entities,
            associations=associations,
            total_entities=total_entities,
        )

    async def entity_context(
        self,
        source_config_id: str,
        entity_id: str,
        *,
        source: Source | None = None,
        limit: int = 20,
    ) -> list[str]:
        """某实体关联事件的文本片段（用于生成人格）。"""
        await self._slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import EventEntity, SourceEvent

        stmt = (
            select(SourceEvent.title, SourceEvent.summary, SourceEvent.content)
            .join(EventEntity, EventEntity.event_id == SourceEvent.id)
            .where(EventEntity.entity_id == entity_id)
            .limit(limit)
        )
        sf = get_session_factory()
        snippets: list[str] = []
        async with sf() as s:
            for title, summary, content in (await s.execute(stmt)).all():
                text = summary or content or title
                if text:
                    snippets.append(str(text)[:500])
        return snippets

    async def universe_overview_stats(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        bucket_count: int = 8,
        category_limit: int = 8,
    ) -> UniverseSourceStatsInfo:
        """Return aggregate-only statistics; never materialize event/entity rows."""
        await self._slot(source_config_id, source)
        from sqlalchemy import case, func, select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        buckets = max(1, min(int(bucket_count), 24))
        categories = max(0, min(int(category_limit), 16))
        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        active_event = SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")
        sf = get_session_factory()
        async with sf() as session:
            event_count, min_time, max_time = (
                await session.execute(
                    select(
                        func.count(SourceEvent.id),
                        func.min(event_time),
                        func.max(event_time),
                    ).where(
                        SourceEvent.source_config_id == source_config_id,
                        active_event,
                    )
                )
            ).one()
            entity_count = int(
                (
                    await session.execute(
                        select(func.count(Entity.id)).where(Entity.source_config_id == source_config_id)
                    )
                ).scalar_one()
                or 0
            )
            unique_relation_rows = (
                select(EventEntity.event_id, EventEntity.entity_id)
                .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                .join(Entity, Entity.id == EventEntity.entity_id)
                .where(
                    SourceEvent.source_config_id == source_config_id,
                    Entity.source_config_id == source_config_id,
                    active_event,
                )
                .distinct()
                .subquery()
            )
            relation_count = int(
                (await session.execute(select(func.count()).select_from(unique_relation_rows))).scalar_one() or 0
            )
            category_rows = []
            if categories:
                category = func.coalesce(func.nullif(SourceEvent.category, ""), "未分类")
                category_rows = (
                    await session.execute(
                        select(category, func.count(SourceEvent.id).label("count"))
                        .where(
                            SourceEvent.source_config_id == source_config_id,
                            active_event,
                        )
                        .group_by(category)
                        .order_by(func.count(SourceEvent.id).desc(), category.asc())
                        .limit(categories)
                    )
                ).all()

            time_buckets: list[UniverseTimeBucketInfo] = []
            if min_time is not None and max_time is not None:
                if max_time <= min_time:
                    time_buckets.append(
                        UniverseTimeBucketInfo(
                            start=min_time,
                            end=max_time,
                            count=int(event_count or 0),
                        )
                    )
                else:
                    step = (max_time - min_time) / buckets
                    boundaries = [min_time + step * index for index in range(buckets + 1)]
                    count_columns = []
                    for index in range(buckets):
                        lower = boundaries[index]
                        upper = boundaries[index + 1]
                        condition = event_time >= lower
                        condition &= event_time <= upper if index == buckets - 1 else event_time < upper
                        count_columns.append(func.sum(case((condition, 1), else_=0)).label(f"bucket_{index}"))
                    bucket_values = (
                        await session.execute(
                            select(*count_columns).where(
                                SourceEvent.source_config_id == source_config_id,
                                active_event,
                            )
                        )
                    ).one()
                    time_buckets = [
                        UniverseTimeBucketInfo(
                            start=boundaries[index],
                            end=boundaries[index + 1],
                            count=int(bucket_values[index] or 0),
                        )
                        for index in range(buckets)
                    ]

        return UniverseSourceStatsInfo(
            event_count=int(event_count or 0),
            entity_count=entity_count,
            relation_count=relation_count,
            category_counts={str(label or "未分类"): int(count or 0) for label, count in category_rows},
            time_buckets=time_buckets,
        )

    async def _universe_entity_event_counts(
        self,
        session: Any,
        source_config_id: str,
        entity_ids: list[str],
        *,
        as_of_db: datetime,
    ) -> dict[str, int]:
        """Count factual source events for a batch of entities at one snapshot."""
        if not entity_ids:
            return {}
        from sqlalchemy import func, select
        from zleap.sag.db.models import EventEntity, SourceEvent

        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        return {
            str(entity_id): int(count or 0)
            for entity_id, count in (
                await session.execute(
                    select(
                        EventEntity.entity_id,
                        func.count(func.distinct(EventEntity.event_id)),
                    )
                    .join(SourceEvent, SourceEvent.id == EventEntity.event_id)
                    .where(
                        EventEntity.entity_id.in_(entity_ids),
                        EventEntity.created_time <= as_of_db,
                        SourceEvent.source_config_id == source_config_id,
                        event_time <= as_of_db,
                        SourceEvent.created_time <= as_of_db,
                        (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                    )
                    .group_by(EventEntity.entity_id)
                )
            ).all()
        }

    async def _universe_event_bundles(
        self,
        session: Any,
        source_config_id: str,
        events: list[dict[str, Any]],
        *,
        as_of_db: datetime,
        entity_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Hydrate event rows and their factual entity relations without N+1 reads."""
        from sqlalchemy import func, select
        from zleap.sag.db.models import Entity, EventEntity

        event_ids = [str(event["id"]) for event in events]
        if not event_ids:
            return [], []

        bounded_entities = max(4, min(int(entity_limit), 8))
        # The API unit is one factual event/entity pair. Normalize at the query
        # boundary so totals, ranking and returned relationships cannot diverge.
        unique_relations = (
            select(
                EventEntity.event_id.label("event_id"),
                EventEntity.entity_id.label("entity_id"),
                func.max(EventEntity.weight).label("weight"),
                func.max(EventEntity.description).label("relation_description"),
            )
            .select_from(EventEntity)
            .join(Entity, Entity.id == EventEntity.entity_id)
            .where(
                EventEntity.event_id.in_(event_ids),
                EventEntity.created_time <= as_of_db,
                Entity.source_config_id == source_config_id,
                Entity.created_time <= as_of_db,
            )
            .group_by(EventEntity.event_id, EventEntity.entity_id)
            .subquery()
        )
        ranked_relations = select(
            unique_relations.c.event_id,
            unique_relations.c.entity_id,
            unique_relations.c.weight,
            unique_relations.c.relation_description,
            func.count().over(partition_by=unique_relations.c.event_id).label("relation_total"),
            func.row_number()
            .over(
                partition_by=unique_relations.c.event_id,
                order_by=(
                    unique_relations.c.weight.desc(),
                    unique_relations.c.entity_id.asc(),
                ),
            )
            .label("relation_rank"),
        ).subquery()
        relation_rows = (
            await session.execute(
                select(
                    ranked_relations.c.event_id,
                    ranked_relations.c.entity_id,
                    ranked_relations.c.weight,
                    ranked_relations.c.relation_description,
                    ranked_relations.c.relation_total,
                    ranked_relations.c.relation_rank,
                    Entity.name,
                    Entity.type,
                    Entity.description.label("entity_description"),
                )
                .join(Entity, Entity.id == ranked_relations.c.entity_id)
                .where(
                    ranked_relations.c.relation_rank <= bounded_entities,
                    Entity.source_config_id == source_config_id,
                )
                .order_by(
                    ranked_relations.c.event_id,
                    ranked_relations.c.relation_rank,
                )
            )
        ).all()

        entity_ids = sorted({str(row.entity_id) for row in relation_rows})
        entity_counts = await self._universe_entity_event_counts(
            session,
            source_config_id,
            entity_ids,
            as_of_db=as_of_db,
        )

        event_counts: dict[str, int] = {}
        entity_nodes: dict[str, dict[str, Any]] = {}
        relations: list[dict[str, Any]] = []
        for row in relation_rows:
            event_id = str(row.event_id)
            entity_id = str(row.entity_id)
            event_counts[event_id] = int(row.relation_total or 0)
            entity_nodes.setdefault(
                entity_id,
                {
                    "id": entity_id,
                    "kind": "entity",
                    "label": row.name or "未命名实体",
                    "description": str(row.entity_description or "")[:800],
                    "category": row.type or "实体",
                    "chunk_id": None,
                    "start_time": None,
                    "importance": max(
                        0.3,
                        min(1.0, 0.42 + _weight(row.weight) * 0.08),
                    ),
                    "related_count": entity_counts.get(entity_id, 0),
                    "state": "active",
                },
            )
            relations.append(
                {
                    "from_id": event_id,
                    "to_id": entity_id,
                    "kind": "mentions",
                    "weight": _weight(row.weight),
                    "description": str(row.relation_description or "")[:240],
                }
            )

        event_nodes = [
            {
                "id": str(event["id"]),
                "kind": "event",
                "label": event.get("title") or "未命名事件",
                "description": str(event.get("summary") or "")[:800],
                "category": event.get("category") or "事件",
                "chunk_id": event.get("chunk_id"),
                "start_time": _utc_time(event.get("start_time") or event.get("event_time")),
                "importance": max(
                    0.4,
                    min(
                        1.0,
                        0.5 + math.log1p(event_counts.get(str(event["id"]), 0)) * 0.08,
                    ),
                ),
                "related_count": event_counts.get(str(event["id"]), 0),
                "state": "active",
            }
            for event in events
        ]
        return [*event_nodes, *entity_nodes.values()], relations

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
        """Return a bidirectional, snapshot-stable event-time page.

        Results always use canonical newest-to-oldest order. ``direction`` only
        selects which adjacent page to read from the supplied boundary cursor.
        """
        await self._slot(source_config_id, source)
        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import SourceEvent

        bounded_limit = max(1, min(int(limit), 24))
        bounded_entities = max(4, min(int(entity_limit), 8))
        if direction not in {"older", "newer"}:
            raise ValueError("invalid universe timeline direction")
        if direction == "newer" and cursor is None:
            raise ValueError("newer universe timeline reads require a cursor")
        if not source_revision:
            raise ValueError("universe source revision is required")
        cursor_payload = _decode_universe_cursor(cursor, self._settings.secret_key) if cursor else None
        snapshot_payload = _decode_universe_cursor(snapshot_id, self._settings.secret_key) if snapshot_id else None
        if cursor_payload and snapshot_payload is None:
            raise ValueError("universe timeline cursor requires a snapshot")
        if cursor_payload and (
            cursor_payload.get("scope") != _universe_cursor_scope(source_config_id)
            or cursor_payload.get("kind") != "source-timeline"
        ):
            raise ValueError("universe cursor does not match its source timeline")
        if snapshot_payload and (
            snapshot_payload.get("scope") != _universe_cursor_scope(source_config_id)
            or snapshot_payload.get("kind") != "source-read-snapshot"
        ):
            raise ValueError("universe snapshot does not match its source")
        if cursor_payload and cursor_payload.get("revision") != source_revision:
            raise ValueError("universe timeline revision changed")
        if snapshot_payload and snapshot_payload.get("revision") != source_revision:
            raise ValueError("universe timeline revision changed")

        if cursor_payload:
            as_of = _cursor_datetime(cursor_payload, "as_of")
        elif snapshot_payload:
            as_of = _cursor_datetime(snapshot_payload, "as_of")
        else:
            as_of = datetime.now(UTC)
        if snapshot_payload and _cursor_datetime(snapshot_payload, "as_of") != as_of:
            raise ValueError("universe cursor does not belong to this snapshot")
        stable_snapshot_id = snapshot_id or _encode_universe_cursor(
            {
                "v": 2,
                "scope": _universe_cursor_scope(source_config_id),
                "kind": "source-read-snapshot",
                "as_of": _utc_time(as_of).isoformat(),
                "revision": source_revision,
            },
            self._settings.secret_key,
        )
        as_of_db = _database_time(as_of)
        event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
        filters = [
            SourceEvent.source_config_id == source_config_id,
            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
            event_time <= as_of_db,
            SourceEvent.created_time <= as_of_db,
        ]
        # Canonical exploration order: newest first, then the extractor's
        # source-wide narrative rank, then id. Rank is what makes a source whose
        # events all share one instant (an imported book) explorable in reading
        # order instead of uuid order — the counting axis keys on this sequence.
        if cursor_payload:
            boundary_time = _database_time(_cursor_datetime(cursor_payload, "time"))
            boundary_rank = _cursor_int(cursor_payload, "rank")
            boundary_id = str(cursor_payload.get("id") or "")
            if boundary_time is None or not boundary_id:
                raise ValueError("invalid universe cursor")
            if direction == "older":
                filters.append(
                    or_(
                        event_time < boundary_time,
                        and_(event_time == boundary_time, SourceEvent.rank > boundary_rank),
                        and_(
                            event_time == boundary_time,
                            SourceEvent.rank == boundary_rank,
                            SourceEvent.id > boundary_id,
                        ),
                    )
                )
            else:
                filters.append(
                    or_(
                        event_time > boundary_time,
                        and_(event_time == boundary_time, SourceEvent.rank < boundary_rank),
                        and_(
                            event_time == boundary_time,
                            SourceEvent.rank == boundary_rank,
                            SourceEvent.id < boundary_id,
                        ),
                    )
                )

        ordering = (
            (event_time.desc(), SourceEvent.rank.asc(), SourceEvent.id.asc())
            if direction == "older"
            else (event_time.asc(), SourceEvent.rank.desc(), SourceEvent.id.desc())
        )

        sf = get_session_factory()
        async with sf() as session:
            event_rows = (
                await session.execute(
                    select(
                        SourceEvent.id,
                        SourceEvent.title,
                        SourceEvent.summary,
                        SourceEvent.category,
                        SourceEvent.chunk_id,
                        SourceEvent.start_time,
                        SourceEvent.rank,
                        event_time.label("event_time"),
                    )
                    .where(*filters)
                    .order_by(*ordering)
                    .limit(bounded_limit + 1)
                )
            ).all()
            has_directional_more = len(event_rows) > bounded_limit
            selected_rows = event_rows[:bounded_limit]
            page = selected_rows if direction == "older" else list(reversed(selected_rows))

            # Ordinals anchor the client's counting axis: within this snapshot,
            # an event's depth is its position in the canonical order. One count
            # for the page head; the page itself is contiguous in that order.
            base_filters = [
                SourceEvent.source_config_id == source_config_id,
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                event_time <= as_of_db,
                SourceEvent.created_time <= as_of_db,
            ]
            total_events = int(
                await session.scalar(
                    select(func.count()).select_from(SourceEvent).where(*base_filters)
                )
                or 0
            )
            first_ordinal = 0
            head = page[0] if page else None
            if head is not None:
                head_time = head.event_time
                head_rank = int(head.rank or 0)
                head_id = str(head.id)
                first_ordinal = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(SourceEvent)
                        .where(
                            *base_filters,
                            or_(
                                event_time > head_time,
                                and_(event_time == head_time, SourceEvent.rank < head_rank),
                                and_(
                                    event_time == head_time,
                                    SourceEvent.rank == head_rank,
                                    SourceEvent.id < head_id,
                                ),
                            ),
                        )
                    )
                    or 0
                )

            bundle_nodes, bundle_relations = await self._universe_event_bundles(
                session,
                source_config_id,
                [
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "summary": row.summary,
                        "category": row.category,
                        "chunk_id": row.chunk_id,
                        "start_time": row.start_time,
                        "event_time": row.event_time,
                    }
                    for row in page
                ],
                as_of_db=as_of_db,
                entity_limit=bounded_entities,
            )

        nodes_by_key = {(str(node.get("kind") or ""), str(node.get("id") or "")): node for node in bundle_nodes}
        relations_by_event: dict[str, list[dict[str, Any]]] = {}
        for relation in bundle_relations:
            relations_by_event.setdefault(str(relation.get("from_id") or ""), []).append(relation)

        def boundary_cursor(row: Any) -> str:
            return _encode_universe_cursor(
                {
                    "v": 2,
                    "scope": _universe_cursor_scope(source_config_id),
                    "kind": "source-timeline",
                    "as_of": _utc_time(as_of).isoformat(),
                    "revision": source_revision,
                    "time": _utc_time(row.event_time).isoformat(),
                    "rank": int(row.rank or 0),
                    "id": row.id,
                },
                self._settings.secret_key,
            )

        has_newer = has_directional_more if direction == "newer" else cursor is not None
        has_older = has_directional_more if direction == "older" else cursor is not None
        bundles: list[UniverseTimelineBundleInfo] = []
        for index, row in enumerate(page):
            event_id = str(row.id)
            event_node = nodes_by_key.get(("event", event_id))
            if event_node is None:
                continue
            relations = relations_by_event.get(event_id, [])
            seen_entity_ids: set[str] = set()
            entity_nodes: list[dict[str, Any]] = []
            for relation in relations:
                entity_id = str(relation.get("to_id") or "")
                if not entity_id or entity_id in seen_entity_ids:
                    continue
                entity_node = nodes_by_key.get(("entity", entity_id))
                if entity_node is None:
                    continue
                seen_entity_ids.add(entity_id)
                entity_nodes.append(entity_node)
            row_cursor = boundary_cursor(row)
            cursor_before = row_cursor if index > 0 or has_newer else None
            cursor_after = row_cursor if index < len(page) - 1 or has_older else None
            neighbor_total = max(0, int(event_node.get("related_count") or 0))
            neighbor_complete = len(entity_nodes) == neighbor_total
            neighbor_next_cursor = None
            if not neighbor_complete and relations:
                last_relation = relations[-1]
                neighbor_next_cursor = _encode_universe_cursor(
                    {
                        "v": 2,
                        "scope": _universe_cursor_scope(source_config_id),
                        "kind": "source-expand",
                        "node_kind": "event",
                        "node": event_id,
                        "as_of": _utc_time(as_of).isoformat(),
                        "revision": source_revision,
                        "weight": format(
                            _weight(last_relation.get("weight")),
                            ".17g",
                        ),
                        "id": str(last_relation.get("to_id") or ""),
                    },
                    self._settings.secret_key,
                )
            bundles.append(
                UniverseTimelineBundleInfo(
                    bundle_id=f"event:{event_id}",
                    ordinal=first_ordinal + index,
                    event=event_node,
                    nodes=entity_nodes,
                    relations=relations,
                    neighbor_total=neighbor_total,
                    neighbor_returned=len(entity_nodes),
                    complete=neighbor_complete,
                    neighbor_next_cursor=neighbor_next_cursor,
                    cursor_before=cursor_before,
                    cursor_after=cursor_after,
                )
            )

        newer_cursor = bundles[0].cursor_before if bundles else cursor if direction == "older" else None
        older_cursor = bundles[-1].cursor_after if bundles else cursor if direction == "newer" else None
        next_cursor = older_cursor if direction == "older" else newer_cursor

        return UniverseTimelineInfo(
            bundles=bundles,
            total_events=total_events,
            snapshot_id=stable_snapshot_id,
            direction=direction,
            has_newer=newer_cursor is not None,
            newer_cursor=newer_cursor,
            has_older=older_cursor is not None,
            older_cursor=older_cursor,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
            as_of=_utc_time(as_of),
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
        """Read one explicit hop with a hard cap and stable keyset cursor."""
        await self._slot(source_config_id, source)
        from sqlalchemy import and_, func, or_, select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, EventEntity, SourceEvent

        hard_limit = 8 if node_kind == "event" else 4
        bounded_limit = max(1, min(int(limit), hard_limit))
        if not source_revision:
            raise ValueError("universe source revision is required")
        cursor_payload = _decode_universe_cursor(cursor, self._settings.secret_key) if cursor else None
        snapshot_payload = _decode_universe_cursor(snapshot_id, self._settings.secret_key) if snapshot_id else None
        if cursor_payload and snapshot_payload is None:
            raise ValueError("universe expansion cursor requires a snapshot")
        if cursor_payload and (
            cursor_payload.get("scope") != _universe_cursor_scope(source_config_id)
            or cursor_payload.get("kind") != "source-expand"
            or cursor_payload.get("node_kind") != node_kind
            or cursor_payload.get("node") != node_id
        ):
            raise ValueError("universe cursor does not match its anchor")
        if snapshot_payload and (
            snapshot_payload.get("scope") != _universe_cursor_scope(source_config_id)
            or snapshot_payload.get("kind") != "source-read-snapshot"
        ):
            raise ValueError("universe snapshot does not match its source")
        if cursor_payload and cursor_payload.get("revision") != source_revision:
            raise ValueError("universe expansion revision changed")
        if snapshot_payload and snapshot_payload.get("revision") != source_revision:
            raise ValueError("universe expansion revision changed")

        if snapshot_payload:
            as_of = _cursor_datetime(snapshot_payload, "as_of")
        elif cursor_payload:
            as_of = _cursor_datetime(cursor_payload, "as_of")
        else:
            as_of = datetime.now(UTC)
        if cursor_payload and _cursor_datetime(cursor_payload, "as_of") != as_of:
            raise ValueError("universe cursor does not belong to this snapshot")
        stable_snapshot_id = snapshot_id or _encode_universe_cursor(
            {
                "v": 2,
                "scope": _universe_cursor_scope(source_config_id),
                "kind": "source-read-snapshot",
                "as_of": _utc_time(as_of).isoformat(),
                "revision": source_revision,
            },
            self._settings.secret_key,
        )

        sf = get_session_factory()
        async with sf() as session:
            as_of_db = _database_time(as_of)
            if node_kind == "event":
                event_time = func.coalesce(
                    SourceEvent.start_time,
                    SourceEvent.created_time,
                )
                anchor = (
                    await session.execute(
                        select(
                            SourceEvent.id,
                            SourceEvent.title,
                            SourceEvent.summary,
                            SourceEvent.category,
                            SourceEvent.chunk_id,
                            SourceEvent.start_time,
                        ).where(
                            SourceEvent.id == node_id,
                            SourceEvent.source_config_id == source_config_id,
                            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                            event_time <= as_of_db,
                            SourceEvent.created_time <= as_of_db,
                        )
                    )
                ).one_or_none()
                if anchor is None:
                    return None
                unique_event_relations = (
                    select(
                        EventEntity.entity_id.label("entity_id"),
                        func.max(EventEntity.weight).label("weight"),
                        func.max(EventEntity.description).label("relation_description"),
                    )
                    .where(
                        EventEntity.event_id == node_id,
                        EventEntity.created_time <= as_of_db,
                    )
                    .group_by(EventEntity.entity_id)
                    .subquery()
                )
                related_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(unique_event_relations)
                            .join(Entity, Entity.id == unique_event_relations.c.entity_id)
                            .where(
                                Entity.source_config_id == source_config_id,
                                Entity.created_time <= as_of_db,
                            )
                        )
                    ).scalar_one()
                    or 0
                )
                filters = []
                if cursor_payload:
                    last_weight = _cursor_float(cursor_payload, "weight")
                    last_id = str(cursor_payload.get("id") or "")
                    if not last_id:
                        raise ValueError("invalid universe cursor")
                    filters.append(
                        or_(
                            unique_event_relations.c.weight < last_weight,
                            and_(
                                unique_event_relations.c.weight == last_weight,
                                unique_event_relations.c.entity_id > last_id,
                            ),
                        )
                    )
                rows = (
                    await session.execute(
                        select(
                            unique_event_relations.c.entity_id,
                            unique_event_relations.c.weight,
                            unique_event_relations.c.relation_description,
                            Entity.name,
                            Entity.type,
                            Entity.description.label("entity_description"),
                        )
                        .select_from(unique_event_relations)
                        .join(Entity, Entity.id == unique_event_relations.c.entity_id)
                        .where(
                            *filters,
                            Entity.source_config_id == source_config_id,
                            Entity.created_time <= as_of_db,
                        )
                        .order_by(
                            unique_event_relations.c.weight.desc(),
                            unique_event_relations.c.entity_id.asc(),
                        )
                        .limit(bounded_limit + 1)
                    )
                ).all()
                has_more = len(rows) > bounded_limit
                page = rows[:bounded_limit]
                entity_counts = await self._universe_entity_event_counts(
                    session,
                    source_config_id,
                    [str(row.entity_id) for row in page],
                    as_of_db=as_of_db,
                )
                next_cursor = None
                if has_more and page:
                    next_cursor = _encode_universe_cursor(
                        {
                            "v": 2,
                            "scope": _universe_cursor_scope(source_config_id),
                            "kind": "source-expand",
                            "node_kind": node_kind,
                            "node": node_id,
                            "as_of": as_of.isoformat(),
                            "revision": source_revision,
                            "weight": format(_weight(page[-1].weight), ".17g"),
                            "id": page[-1].entity_id,
                        },
                        self._settings.secret_key,
                    )
                return UniverseExpansionInfo(
                    anchor={
                        "id": anchor.id,
                        "kind": "event",
                        "label": anchor.title or "未命名事件",
                        "description": str(anchor.summary or "")[:800],
                        "category": anchor.category or "事件",
                        "chunk_id": anchor.chunk_id,
                        "start_time": anchor.start_time,
                        "related_count": related_count,
                    },
                    neighbors=[
                        {
                            "id": row.entity_id,
                            "kind": "entity",
                            "label": row.name or "未命名实体",
                            "description": str(row.entity_description or "")[:800],
                            "category": row.type or "实体",
                            "weight": _weight(row.weight),
                            "related_count": entity_counts.get(str(row.entity_id), 0),
                            "relation_description": str(row.relation_description or "")[:240],
                        }
                        for row in page
                    ],
                    relations=[
                        {
                            "from_id": str(anchor.id),
                            "to_id": str(row.entity_id),
                            "kind": "mentions",
                            "weight": _weight(row.weight),
                            "description": str(row.relation_description or "")[:240],
                        }
                        for row in page
                    ],
                    returned=len(page),
                    has_more=has_more,
                    next_cursor=next_cursor,
                    snapshot_id=stable_snapshot_id,
                    as_of=as_of,
                )

            if node_kind != "entity":
                return None
            anchor = (
                await session.execute(
                    select(Entity.id, Entity.name, Entity.type, Entity.description).where(
                        Entity.id == node_id,
                        Entity.source_config_id == source_config_id,
                        Entity.created_time <= as_of_db,
                    )
                )
            ).one_or_none()
            if anchor is None:
                return None

            event_time = func.coalesce(SourceEvent.start_time, SourceEvent.created_time)
            unique_entity_relations = (
                select(
                    EventEntity.event_id.label("event_id"),
                    func.max(EventEntity.weight).label("weight"),
                    func.max(EventEntity.description).label("relation_description"),
                )
                .where(
                    EventEntity.entity_id == node_id,
                    EventEntity.created_time <= as_of_db,
                )
                .group_by(EventEntity.event_id)
                .subquery()
            )
            effective_after = after
            base_filters = [
                SourceEvent.source_config_id == source_config_id,
                (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                event_time <= as_of_db,
                SourceEvent.created_time <= as_of_db,
            ]
            after_db = _database_time(effective_after)
            before_db = _database_time(before)
            expected_after = after_db.isoformat() if after_db is not None else None
            expected_before = before_db.isoformat() if before_db is not None else None
            if cursor_payload and (
                cursor_payload.get("after") != expected_after or cursor_payload.get("before") != expected_before
            ):
                raise ValueError("universe cursor does not match its time range")
            if after_db is not None:
                base_filters.append(event_time >= after_db)
            if before_db is not None:
                base_filters.append(event_time <= before_db)
            related_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(unique_entity_relations)
                        .join(
                            SourceEvent,
                            SourceEvent.id == unique_entity_relations.c.event_id,
                        )
                        .where(*base_filters)
                    )
                ).scalar_one()
                or 0
            )
            filters = list(base_filters)
            if cursor_payload:
                last_time = _cursor_datetime(cursor_payload, "time")
                last_time_db = _database_time(last_time)
                last_weight = _cursor_float(cursor_payload, "weight")
                last_id = str(cursor_payload.get("id") or "")
                if not last_id:
                    raise ValueError("invalid universe cursor")
                filters.append(
                    or_(
                        event_time < last_time_db,
                        and_(
                            event_time == last_time_db,
                            unique_entity_relations.c.weight < last_weight,
                        ),
                        and_(
                            event_time == last_time_db,
                            unique_entity_relations.c.weight == last_weight,
                            unique_entity_relations.c.event_id < last_id,
                        ),
                    )
                )
            rows = (
                await session.execute(
                    select(
                        unique_entity_relations.c.event_id,
                        unique_entity_relations.c.weight,
                        unique_entity_relations.c.relation_description,
                        SourceEvent.title,
                        SourceEvent.summary,
                        SourceEvent.category,
                        SourceEvent.chunk_id,
                        SourceEvent.start_time,
                        event_time.label("event_time"),
                    )
                    .select_from(unique_entity_relations)
                    .join(
                        SourceEvent,
                        SourceEvent.id == unique_entity_relations.c.event_id,
                    )
                    .where(*filters)
                    .order_by(
                        event_time.desc(),
                        unique_entity_relations.c.weight.desc(),
                        unique_entity_relations.c.event_id.desc(),
                    )
                    .limit(bounded_limit + 1)
                )
            ).all()
            has_more = len(rows) > bounded_limit
            page = rows[:bounded_limit]
            next_cursor = None
            if has_more and page:
                next_cursor = _encode_universe_cursor(
                    {
                        "v": 2,
                        "scope": _universe_cursor_scope(source_config_id),
                        "kind": "source-expand",
                        "node_kind": node_kind,
                        "node": node_id,
                        "as_of": as_of.isoformat(),
                        "revision": source_revision,
                        "after": expected_after,
                        "before": expected_before,
                        "time": page[-1].event_time.isoformat(),
                        "weight": format(_weight(page[-1].weight), ".17g"),
                        "id": page[-1].event_id,
                    },
                    self._settings.secret_key,
                )
            bundle_nodes, bundle_relations = await self._universe_event_bundles(
                session,
                source_config_id,
                [
                    {
                        "id": str(row.event_id),
                        "title": row.title,
                        "summary": row.summary,
                        "category": row.category,
                        "chunk_id": row.chunk_id,
                        "start_time": row.start_time,
                        "event_time": row.event_time,
                    }
                    for row in page
                ],
                as_of_db=as_of_db,
                entity_limit=self._settings.universe_event_entity_limit,
            )
            direct_event_ids = {
                str(relation.get("from_id") or "")
                for relation in bundle_relations
                if str(relation.get("to_id") or "") == node_id
            }
            for row in page:
                event_id = str(row.event_id)
                if event_id in direct_event_ids:
                    continue
                bundle_relations.append(
                    {
                        "from_id": event_id,
                        "to_id": node_id,
                        "kind": "mentions",
                        "weight": _weight(row.weight),
                        "description": str(row.relation_description or "")[:240],
                    }
                )
            return UniverseExpansionInfo(
                anchor={
                    "id": anchor.id,
                    "kind": "entity",
                    "label": anchor.name or "未命名实体",
                    "description": str(anchor.description or "")[:800],
                    "category": anchor.type or "实体",
                    "related_count": related_count,
                },
                neighbors=[
                    node for node in bundle_nodes if not (node.get("kind") == "entity" and node.get("id") == node_id)
                ],
                relations=bundle_relations,
                returned=len(page),
                has_more=has_more,
                next_cursor=next_cursor,
                snapshot_id=stable_snapshot_id,
                as_of=as_of,
            )

    async def universe_node_detail(
        self,
        source_config_id: str,
        node_kind: str,
        node_id: str,
        *,
        source: Source | None = None,
    ) -> dict[str, Any] | None:
        """Read node metadata only; graph neighborhoods are served by universe_expand."""
        await self._slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Entity, SourceEvent

        sf = get_session_factory()
        async with sf() as session:
            if node_kind == "event":
                event = (
                    await session.execute(
                        select(
                            SourceEvent.id,
                            SourceEvent.source_id,
                            SourceEvent.title,
                            SourceEvent.summary,
                            SourceEvent.content,
                            SourceEvent.category,
                            SourceEvent.chunk_id,
                            SourceEvent.start_time,
                        ).where(
                            SourceEvent.id == node_id,
                            SourceEvent.source_config_id == source_config_id,
                            (SourceEvent.status.is_(None) | (SourceEvent.status != "DELETED")),
                        )
                    )
                ).one_or_none()
                if event is None:
                    return None
                summary = str(event.summary or "").strip()
                content = str(event.content or "").strip()
                return {
                    "id": event.id,
                    "kind": "event",
                    "source_ref_id": event.source_id,
                    "label": event.title or "未命名事件",
                    # summary is the compact graph-card copy; content is the
                    # extracted event detail shown after opening the node.
                    "description": (content or summary)[:4000],
                    "category": event.category or "",
                    "chunk_id": event.chunk_id,
                    "start_time": event.start_time,
                }

            if node_kind != "entity":
                return None
            entity = (
                await session.execute(
                    select(Entity.id, Entity.name, Entity.type, Entity.description).where(
                        Entity.id == node_id,
                        Entity.source_config_id == source_config_id,
                    )
                )
            ).one_or_none()
            if entity is None:
                return None
            return {
                "id": entity.id,
                "kind": "entity",
                "label": entity.name or "未命名实体",
                "description": str(entity.description or "")[:4000],
                "category": entity.type or "实体",
            }

    async def list_chunk_headings(
        self,
        source_config_id: str,
        *,
        source: Source | None = None,
        doc_sag_id: str | None = None,
        limit: int = 300,
    ) -> list[dict]:
        """分块大纲：heading + rank（可限定单文档），供 MCP outline。"""
        await self._slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import SourceChunk

        conds = [SourceChunk.source_config_id == source_config_id]
        if doc_sag_id:
            conds.append(SourceChunk.source_id == doc_sag_id)
        stmt = (
            select(SourceChunk.id, SourceChunk.heading, SourceChunk.rank)
            .where(*conds)
            .order_by(SourceChunk.rank)
            .limit(limit)
        )
        sf = get_session_factory()
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        return [{"chunk_id": cid, "heading": (h or "").strip(), "rank": int(r or 0)} for cid, h, r in rows]

    async def get_document_markdown(
        self,
        source_config_id: str,
        article_id: str,
        *,
        source: Source | None = None,
    ) -> str | None:
        """读取成功入库时保存的整篇 Markdown；不存在或内容为空时返回 None。"""
        await self._slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import Article

        sf = get_session_factory()
        async with sf() as session:
            content = await session.scalar(
                select(Article.content).where(
                    Article.id == article_id,
                    Article.source_config_id == source_config_id,
                )
            )
        return str(content) if content else None

    async def grep_chunks(
        self,
        source_config_id: str,
        pattern: str,
        *,
        source: Source | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """精确文本匹配（LIKE，大小写不敏感）：语义检索之外的确定性查找。"""
        await self._ensure_read_runtime({source_config_id: source})
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import SourceChunk

        needle = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = (
            select(SourceChunk.id, SourceChunk.heading, SourceChunk.content)
            .where(
                SourceChunk.source_config_id == source_config_id,
                SourceChunk.content.ilike(f"%{needle}%", escape="\\"),
            )
            .order_by(SourceChunk.rank)
            .limit(limit)
        )
        sf = get_session_factory()
        async with sf() as s:
            rows = (await s.execute(stmt)).all()
        out = []
        for cid, heading, content in rows:
            text = content or ""
            lowered = text.lower()
            needle = pattern.lower()
            positions: list[int] = []
            start = 0
            while (position := lowered.find(needle, start)) >= 0:
                positions.append(position)
                start = position + max(1, len(needle))

            def quality(position: int, content: str = text) -> int:
                window = content[max(0, position - 40) : position + 700]
                cjk = sum("\u3400" <= char <= "\u9fff" for char in window)
                return cjk - window.count("http") * 40 - window.count("](") * 15

            best = max(positions, key=quality) if positions else 0
            prefix_start = max(0, best - 500)
            prefix = text[prefix_start:best]
            dates = list(
                re.finditer(
                    r"20\d{2}(?:年\d{1,2}月\d{1,2}日|[-/]\d{1,2}[-/]\d{1,2})",
                    prefix,
                )
            )
            lo = prefix_start + dates[-1].start() if dates else max(0, best - 80)
            snippet = text[lo : best + 700]
            snippet = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", snippet)
            snippet = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", snippet)
            snippet = re.sub(r"[ \t]+", " ", snippet).strip()
            display_heading = (heading or "").strip()
            for line in text.splitlines():
                candidate = re.sub(r"!\[[^]]*\]\([^)]*\)", "", line)
                candidate = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", candidate)
                candidate = candidate.lstrip("#* -").strip()
                if needle in candidate.lower() and 2 <= len(candidate) <= 160:
                    display_heading = candidate
                    break
            out.append(
                {
                    "chunk_id": cid,
                    "heading": display_heading,
                    "snippet": snippet,
                }
            )
        return out

    async def get_chunk(
        self,
        source_config_id: str,
        chunk_id: str,
        *,
        source: Source | None = None,
    ):
        """读取某分块的完整原文（引用/搜索溯源）。不存在返回 None。"""
        from sag_api.sag.dto import ChunkInfo

        await self._slot(source_config_id, source)
        from sqlalchemy import select
        from zleap.sag.db import get_session_factory
        from zleap.sag.db.models import SourceChunk

        sf = get_session_factory()
        async with sf() as s:
            row = (
                await s.execute(
                    select(SourceChunk).where(
                        SourceChunk.id == chunk_id,
                        SourceChunk.source_config_id == source_config_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return ChunkInfo(
            chunk_id=row.id,
            heading=(row.heading or "").strip(),
            content=(row.content or row.raw_content or "").strip(),
            rank=int(row.rank or 0),
        )

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
