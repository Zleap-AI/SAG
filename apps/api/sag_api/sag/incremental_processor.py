"""zleap-sag 0.8.2 管线适配层。

0.8.2 将 load/extract 收敛为显式管线,本模块是 SAG 文档处理(ingest+extract)
与 zleap 管线公开 API 之间的适配:

- ``engine.ingest(...)``:Parse → Chunk → Index,返回 ``ChunkSetRef``
  (含 source_id / chunk_ids / generation_id / chunk_version);
- ``engine.extract(ref, ExtractionOptions, observer, cancellation)``:
  整批抽取,返回 ``EventSetRef``(event_ids / entity_ids / stats)。

SAG 职责(保留):断点持久化、暂停/取消、进度映射、与 Job 系统的对接。
zleap 职责(0.8.2 已内置,不再由 SAG 实现):实体数契约
(``ExtractionLimits.min_entities_per_event=1``)、修复重试(``max_retries``)、
无效事项过滤(``is_valid``)、代际替换语义。

临时行为差异(与 0.7.1 相比,带 REQ 标记,待 zleap 需求落地后消除):

- REQ-1/2(事件粒度过滤与修复):0.8.2 的契约校验与修复重试是批次粒度,
  SAG 不再做逐事件过滤(0.7.1 的 ``_require_event_entities`` /
  ``guarded_save_events`` monkeypatch 已删除);一个 chunk 修复耗尽会拒绝整批。
- REQ-3(逐块断点):0.8.2 一次 extract 覆盖全部 chunks(单代提交),
  SAG 不再逐块持久化断点;暂停/取消后恢复会整批重跑(LLM 成本回归)。
  zleap 提供代际 durable prepare/commit 公开入口后恢复逐块断点。
- REQ-4/5(schema 强化与 SQLite int64 防护):0.7.1 的
  ``_strengthen_event_entity_schema`` / ``_install_sqlite_integer_guard``
  monkeypatch 目标在 0.8.2 已不可达,shim 删除,待 zleap 内置。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from zleap.sag import DataEngine
from zleap.sag.pipeline import (
    CancellationToken,
    ChunkOptions,
    ChunkSetRef,
    ExtractionExecutionOptions,
    ExtractionLimits,
    ExtractionOptions,
    IndexOptions,
    SourceDescriptor,
    SourceType,
    WriteStatus,
)
from zleap.sag.pipeline.errors import PipelineCancelledError
from zleap.sag.pipeline.events import StageEvent, StageEventType, StageName

from sag_api.core.logging import get_logger
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

CheckpointCallback = Callable[[ProcessCheckpoint], Awaitable[None]]
PauseCheck = Callable[[], Awaitable[bool]]
StageCallback = Callable[[str], Awaitable[None]]

log = get_logger("sag.incremental")

_KNOWLEDGE_EVENT_REQUIREMENTS = {
    "zh": (
        '对于书籍、报告、论文等非新闻文档，"事项"也包括可独立理解的观点、事实、定义、\n'
        "机制、因果关系、论证和结论，不要求必须包含日期、人物动作或新闻事件。\n"
        "只有目录、页眉页脚、广告、乱码、纯链接，或确实与文档主题无关的片段才可返回空结果；\n"
        "正文只要包含可复用的知识，就至少保留一个有效的顶级事项。\n"
        '每个实体必须严格使用 {"type":"实体类型","name":"实体名称","description":"作用说明"}；\n'
        "禁止把实体类型写成字段名，例如不能输出\n"
        '{"location":"中东","name":"中东","description":"地区"}。'
    ),
    "en": (
        'For books, reports, papers, and other non-news documents, an "event" also includes\n'
        "independently understandable viewpoints, facts, definitions, mechanisms, causal\n"
        "relationships, arguments, and conclusions. It does not need to include a date, a person's\n"
        "action, or a news event.\n"
        "Only tables of contents, headers, footers, advertisements, corrupted text, standalone links,\n"
        "or fragments genuinely unrelated to the document topic may return an empty result. Retain at\n"
        "least one valid top-level event whenever the main text contains reusable knowledge.\n"
        "Every entity must strictly use\n"
        '{"type":"entity type","name":"entity name","description":"role description"}.\n'
        "Do not use an entity type as a field name. For example, do not output\n"
        '{"location":"Middle East","name":"Middle East","description":"region"}.'
    ),
}

# 进度观察节流:避免把 zleap 的每个 progress 事件都转换成一次 DB 断点写入。
_PROGRESS_COMMIT_EVERY = 5


class IncrementalDocumentProcessor:
    def __init__(
        self,
        engine: DataEngine,
        source_config_id: str,
        *,
        max_concurrency: int,
        chunk_max_tokens: int = 1_000,
        chunk_mode: str = "standard",
        document_title: str | None = None,
        max_entities_per_event: int = 20,
        enable_strict_filtering: bool = False,
        event_entity_attempts: int = 2,
    ) -> None:
        self._engine = engine
        self._source_config_id = source_config_id
        self._max_concurrency = max(1, min(100, max_concurrency))
        self._chunk_max_tokens = chunk_max_tokens
        self._chunk_mode = chunk_mode if chunk_mode in {"standard", "heading_strict"} else "standard"
        self._document_title = (document_title or "").strip()
        self._max_entities_per_event = max(1, min(20, max_entities_per_event))

        # 0.8.2 已内置契约与修复重试,以下 0.7.1 参数仅保留接口兼容:
        # - enable_strict_filtering → zleap 无对应开关(REQ 待对齐),忽略并记录;
        # - event_entity_attempts → zleap ExtractionOptions.max_retries(默认 5)接管。
        if enable_strict_filtering:
            log.warning("0.8.2 无严格过滤开关,忽略 enable_strict_filtering(REQ 待对齐)")
        self._event_entity_attempts = max(1, min(3, event_entity_attempts))

    # ── 主流程 ────────────────────────────────────────────────────────────
    async def process(
        self,
        path: str | Path | None,
        *,
        checkpoint: ProcessCheckpoint,
        on_checkpoint: CheckpointCallback,
        should_pause: PauseCheck,
        on_stage: StageCallback | None = None,
    ) -> ProcessOutcome:
        current = checkpoint.model_copy(deep=True)

        # 阶段 1:解析 → 切块 → 落库(仅在断点尚未建立时)
        if not current.chunk_ids:
            if path is None:
                raise RuntimeError("文档尚未切片，无法从断点继续")
            if on_stage:
                await on_stage("loading")
            await self._ingest(current, path, on_checkpoint)

        chunk_set = self._chunk_set_ref(current)

        # 阶段 2:整批抽取(0.8.2 批次粒度;REQ-3 落地前不逐块断点)
        if on_stage:
            await on_stage("extracting")
        cancelled, events = await self._extract(current, chunk_set, should_pause, on_checkpoint)
        paused = cancelled or bool(current.processed_chunk_ids) and len(current.processed_chunk_ids) < len(
            current.chunk_ids
        )

        if not cancelled and events is not None:
            current.event_ids = list(events.event_ids)
            current.event_count = events.event_count
            current.processed_chunk_ids = list(chunk_set.chunk_ids)
            await on_checkpoint(current.model_copy(deep=True))

        return ProcessOutcome(
            source_id=current.source_id,
            chunk_count=len(current.chunk_ids),
            event_count=current.event_count,
            chunk_ids=list(current.chunk_ids),
            event_ids=list(current.event_ids),
            processed_chunk_ids=list(current.processed_chunk_ids),
            eventless_chunk_ids=list(current.eventless_chunk_ids),
            token_usage=current.token_usage,
            paused=paused,
        )

    async def _ingest(
        self,
        current: ProcessCheckpoint,
        path: str | Path,
        on_checkpoint: CheckpointCallback,
    ) -> None:
        """Parse → Chunk → Index;把 ChunkSetRef 的定位信息固化进断点。"""
        descriptor = SourceDescriptor(
            source_type=SourceType.ARTICLE,
            title=self._document_title or None,
        )
        chunk_set = await self._engine.ingest(
            str(path),
            descriptor=descriptor,
            chunk_options=ChunkOptions(
                strategy=self._chunk_mode,  # type: ignore[arg-type]
                max_tokens=self._chunk_max_tokens,
            ),
            index_options=IndexOptions(),
        )
        current.source_id = chunk_set.source_id
        current.chunk_ids = list(chunk_set.chunk_ids)
        current.generation_id = chunk_set.generation_id
        current.chunk_version = chunk_set.chunk_version
        current.source_version = chunk_set.source_version
        current.processed_chunk_ids = []
        current.event_count = 0
        current.event_ids = []
        current.eventless_chunk_ids = []
        current.token_usage = 0
        await on_checkpoint(current.model_copy(deep=True))
        log.info(
            "文档切片完成 source_config_id=%s source_id=%s chunks=%d generation=%s",
            self._source_config_id,
            current.source_id,
            len(current.chunk_ids),
            current.generation_id,
        )

    def _chunk_set_ref(self, current: ProcessCheckpoint) -> ChunkSetRef:
        """从断点重建 ChunkSetRef；普通 ingest 的 generation_id 可为空。"""
        if not current.chunk_version:
            raise RuntimeError("断点缺少 chunk_version，无法重建 ChunkSetRef；请重新处理文档")
        if current.source_id is None:
            raise RuntimeError("断点缺少 source_id，无法重建 ChunkSetRef")
        return ChunkSetRef(
            data_source_id=self._source_config_id,
            source_type=SourceType.ARTICLE,
            source_id=current.source_id,
            source_version=current.source_version or "",
            chunk_version=current.chunk_version,
            generation_id=current.generation_id,
            chunk_ids=tuple(current.chunk_ids),
            client_key_to_chunk_id={},
            relation_status=WriteStatus.SUCCEEDED,
            vector_status=WriteStatus.SUCCEEDED,
        )

    async def _extract(
        self,
        current: ProcessCheckpoint,
        chunk_set: ChunkSetRef,
        should_pause: PauseCheck,
        on_checkpoint: CheckpointCallback,
    ) -> tuple[bool, Any]:
        """整批抽取;暂停由 CancellationToken + 后台轮询驱动,进度经 observer 透出。"""
        prompt_language = getattr(getattr(self._engine.resources, "prompts", None), "language", None)
        requirements = _KNOWLEDGE_EVENT_REQUIREMENTS.get(prompt_language)
        if requirements is None:
            raise RuntimeError(f"不支持的抽取提示词语言: {prompt_language!r}")

        options = ExtractionOptions(
            source_type="article",
            contract="rich",
            limits=ExtractionLimits(
                max_events_per_chunk=20,
                min_entities_per_event=1,
                max_entities_per_event=self._max_entities_per_event,
            ),
            execution=ExtractionExecutionOptions(max_concurrency=self._max_concurrency),
            guidance_rules=(requirements,),
        )
        cancellation = CancellationToken()

        async def poll_pause() -> None:
            while not cancellation.is_cancelled:
                try:
                    if await should_pause():
                        cancellation.cancel()
                        return
                except Exception:  # noqa: BLE001 - 暂停探测失败按不暂停处理
                    pass
                await asyncio.sleep(1.0)

        poller = asyncio.create_task(poll_pause())
        last_committed_progress = -1

        async def observer(event: StageEvent) -> None:
            nonlocal last_committed_progress
            if event.stage != StageName.EXTRACT or event.type != StageEventType.PROGRESS:
                return
            completed = event.completed or 0
            total = event.total or len(chunk_set.chunk_ids)
            if completed < last_committed_progress:
                return
            current.processed_chunk_ids = list(chunk_set.chunk_ids[:completed])
            if total and (completed % _PROGRESS_COMMIT_EVERY == 0 or completed >= total):
                last_committed_progress = completed
                await on_checkpoint(current.model_copy(deep=True))

        try:
            events = await self._engine.extract(
                chunk_set,
                options,
                observer=observer,
                cancellation=cancellation,
            )
        except PipelineCancelledError:
            log.info("抽取已取消 source_config_id=%s", self._source_config_id)
            return True, None
        except asyncio.CancelledError:
            raise
        finally:
            poller.cancel()
            try:
                await poller
            except BaseException:  # noqa: BLE001 - 取消后的 CancelledError 也属预期
                pass

        stats = dict(getattr(events, "stats", {}) or {})
        current.token_usage = int(stats.get("token_usage", 0) or 0)
        # 0.8.2 统计含 zero_event_chunks(整批抽取下语义与 0.7.1 的逐块 eventless 对齐)
        zero_chunks = stats.get("zero_event_chunks", [])
        if isinstance(zero_chunks, (list, tuple)):
            current.eventless_chunk_ids = [str(item) for item in zero_chunks]
        return False, events
