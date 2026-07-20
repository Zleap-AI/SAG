"""zleap-sag 的并发、进度和断点适配层。

上游 DataEngine 只暴露整篇 extract；这里把抽取拆成独立 chunk 任务，
每个 chunk 保存成功后立即持久化断点，暂停或重试时从最近确认的断点继续。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from zleap.sag import DataEngine
from zleap.sag.modules.extract.config import ExtractConfig
from zleap.sag.modules.extract.extractor import EventExtractor
from zleap.sag.modules.load.config import DocumentLoadConfig
from zleap.sag.modules.load.loader import DocumentLoader
from zleap.sag.modules.load.parser import MarkdownParser

from sag_api.core.logging import get_logger
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

CheckpointCallback = Callable[[ProcessCheckpoint], Awaitable[None]]
PauseCheck = Callable[[], Awaitable[bool]]
StageCallback = Callable[[str], Awaitable[None]]

log = get_logger("sag.incremental")

_KNOWLEDGE_EVENT_REQUIREMENTS = """
对于书籍、报告、论文等非新闻文档，“事项”也包括可独立理解的观点、事实、定义、
机制、因果关系、论证和结论，不要求必须包含日期、人物动作或新闻事件。
只有目录、页眉页脚、广告、乱码、纯链接，或确实与文档主题无关的片段才可返回空结果；
正文只要包含可复用的知识，就至少保留一个有效的顶级事项。
每个实体必须严格使用 {"type":"实体类型","name":"实体名称","description":"作用说明"}；
禁止把实体类型写成字段名，例如不能输出
{"location":"中东","name":"中东","description":"地区"}。
""".strip()


class _FallbackTitleMarkdownParser(MarkdownParser):
    """Preserve Muse's logical filename when converted Markdown has no H1."""

    def __init__(self, fallback_title: str) -> None:
        super().__init__()
        self._fallback_title = fallback_title.strip()

    def extract_title(self, content: str) -> str:
        title = super().extract_title(content)
        if title.strip().casefold() == "untitled" and self._fallback_title:
            return self._fallback_title
        return title


def _llm_chat_owner(client: Any) -> Any:
    """找到真正执行 chat 的最内层 zleap-sag 客户端。"""
    current = client
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        nested = getattr(current, "client", None)
        if nested is None or not callable(getattr(nested, "chat", None)):
            break
        current = nested
    return current


def _usage_value(value: Any, field: str) -> int:
    raw = value.get(field, 0) if isinstance(value, Mapping) else getattr(value, field, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _response_token_usage(response: Any) -> int:
    for value in (
        response,
        getattr(response, "usage", None),
        getattr(response, "usage_metadata", None),
    ):
        if value is None:
            continue
        total = _usage_value(value, "total_tokens")
        if total > 0:
            return total
        input_tokens = _usage_value(value, "prompt_tokens") or _usage_value(value, "input_tokens")
        output_tokens = _usage_value(value, "completion_tokens") or _usage_value(value, "output_tokens")
        if input_tokens + output_tokens > 0:
            return input_tokens + output_tokens
    return 0


def _entity_types_from_messages(messages: object) -> set[str]:
    """Read the current extraction request's explicit entity-type vocabulary."""

    if not isinstance(messages, list):
        return set()
    for message in reversed(messages):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        meta = data.get("meta") if isinstance(data, dict) else None
        entity_types = meta.get("entity_types") if isinstance(meta, dict) else None
        if not isinstance(entity_types, list):
            continue
        return {
            item["type"].strip()
            for item in entity_types
            if isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"].strip()
        }
    return set()


def _normalize_event_entity_aliases(event: object, allowed_types: set[str]) -> int:
    """Normalize only an unambiguous model typo before SAG validates schema.

    Some OpenAI-compatible models occasionally emit
    ``{"location": "中东", "name": "中东", ...}`` instead of putting
    ``location`` in the required ``type`` field.  We only rewrite when there
    is exactly one unexpected key, that key is in this request's allowed type
    vocabulary, and its value equals ``name``; ambiguous or incomplete objects
    remain untouched and will still fail SAG validation.
    """

    if not isinstance(event, dict):
        return 0
    normalized = 0
    entities = event.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict) or "type" in entity:
                continue
            name = entity.get("name")
            description = entity.get("description")
            if not isinstance(name, str) or not isinstance(description, str):
                continue
            aliases = [key for key in entity if key not in {"name", "description"}]
            if len(aliases) != 1:
                continue
            alias = aliases[0]
            alias_value = entity.get(alias)
            if not isinstance(alias, str) or alias.strip() not in allowed_types:
                continue
            if not isinstance(alias_value, str) or alias_value.strip() != name.strip():
                continue
            entity.pop(alias)
            entity["type"] = alias.strip()
            normalized += 1

    children = event.get("children")
    if isinstance(children, list):
        for child in children:
            normalized += _normalize_event_entity_aliases(child, allowed_types)
    return normalized


def _normalize_extraction_response(response: Any, allowed_types: set[str]) -> int:
    """Apply the narrow entity-key compatibility rule to one LLM response."""

    if not allowed_types:
        return 0
    content = getattr(response, "content", None)
    if not isinstance(content, str):
        return 0
    candidate = content.strip()
    fenced = candidate.startswith("```") and candidate.endswith("```")
    if fenced:
        lines = candidate.splitlines()
        if len(lines) < 3 or lines[0].strip().casefold() not in {"```", "```json"}:
            return 0
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return 0

    normalized = sum(_normalize_event_entity_aliases(item, allowed_types) for item in items)
    if normalized:
        response.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return normalized


def _first_task_error(group: BaseExceptionGroup) -> Exception:
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            return _first_task_error(error)
        if isinstance(error, Exception):
            return error
    return RuntimeError(str(group))


class IncrementalDocumentProcessor:
    def __init__(
        self,
        engine: DataEngine,
        source_config_id: str,
        *,
        max_concurrency: int,
        chunk_max_tokens: int = 1_000,
        chunk_mode: Literal["standard", "heading_strict"] = "standard",
        document_title: str | None = None,
        enable_strict_filtering: bool = False,
    ) -> None:
        self._engine = engine
        self._source_config_id = source_config_id
        self._max_concurrency = max(1, min(100, max_concurrency))
        self._chunk_max_tokens = chunk_max_tokens
        self._chunk_mode = chunk_mode
        self._document_title = (document_title or "").strip()
        self._enable_strict_filtering = enable_strict_filtering

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
        if not current.chunk_ids:
            if path is None:
                raise RuntimeError("文档尚未切片，无法从断点继续")
            if on_stage:
                await on_stage("loading")
            loader = (
                DocumentLoader(parser=_FallbackTitleMarkdownParser(self._document_title))
                if self._document_title
                else DocumentLoader()
            )
            loaded = await loader.load(
                DocumentLoadConfig(
                    path=str(path),
                    source_config_id=self._source_config_id,
                    max_tokens=self._chunk_max_tokens,
                    chunk_mode=self._chunk_mode,
                )
            )
            current.source_id = getattr(loaded, "source_id", None)
            current.chunk_ids = list(getattr(loaded, "chunk_ids", []) or [])
            current.processed_chunk_ids = []
            current.event_count = 0
            current.event_ids = []
            current.eventless_chunk_ids = []
            current.token_usage = 0
            await on_checkpoint(current.model_copy(deep=True))

        if on_stage:
            await on_stage("extracting")

        processed = set(current.processed_chunk_ids)
        remaining = [chunk_id for chunk_id in current.chunk_ids if chunk_id not in processed]
        if remaining and not await should_pause():
            await self._extract_remaining(
                remaining,
                current=current,
                on_checkpoint=on_checkpoint,
                should_pause=should_pause,
            )

        await self._restore_checkpoint_events(current.event_ids)
        paused = len(current.processed_chunk_ids) < len(current.chunk_ids)
        if not paused:
            await self._normalize_event_ranks(current.chunk_ids)
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

    async def _extract_remaining(
        self,
        chunk_ids: list[str],
        *,
        current: ProcessCheckpoint,
        on_checkpoint: CheckpointCallback,
        should_pause: PauseCheck,
    ) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue()
        for chunk_id in chunk_ids:
            queue.put_nowait(chunk_id)
        checkpoint_lock = asyncio.Lock()

        async def worker() -> None:
            while not queue.empty():
                if await should_pause():
                    return
                try:
                    chunk_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    event_ids, token_usage = await self._extract_chunk(chunk_id)
                    async with checkpoint_lock:
                        if chunk_id in current.processed_chunk_ids:
                            continue
                        current.processed_chunk_ids.append(chunk_id)
                        current.event_ids.extend(event_ids)
                        current.event_count += len(event_ids)
                        if event_ids:
                            if chunk_id in current.eventless_chunk_ids:
                                current.eventless_chunk_ids.remove(chunk_id)
                        elif chunk_id not in current.eventless_chunk_ids:
                            current.eventless_chunk_ids.append(chunk_id)
                        current.token_usage += token_usage
                        # zleap-sag replaces an article's visible event set on
                        # every chunk save. Restore the complete checkpoint
                        # before publishing its counters so `/graph` can read
                        # every event the document detail has just announced.
                        await self._restore_checkpoint_events(current.event_ids)
                        await on_checkpoint(current.model_copy(deep=True))
                finally:
                    queue.task_done()

        worker_count = min(self._max_concurrency, len(chunk_ids))
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(worker_count):
                    group.create_task(worker())
        except ExceptionGroup as errors:
            # TaskGroup 会把单块的 SAG/LLM 异常包成通用 ExceptionGroup；解包后
            # EngineManager 才能映射可重试类型，文档与 Job 也能保存真实错误原因。
            raise _first_task_error(errors) from errors

    async def _extract_chunk(self, chunk_id: str) -> tuple[list[str], int]:
        template = getattr(self._engine, "_extractor", None)
        if template is None:
            raise RuntimeError("抽取引擎尚未初始化")
        extractor = EventExtractor(
            prompt_manager=template.prompt_manager,
            model_config=template.model_config,
        )

        token_usage = 0
        chunk_failure: Exception | None = None
        client = await extractor._get_llm_client()
        chat_owner = _llm_chat_owner(client)
        original_chat = chat_owner.chat

        async def tracked_chat(*args: Any, **kwargs: Any):
            nonlocal token_usage
            response = await original_chat(*args, **kwargs)
            used = _response_token_usage(response)
            if used <= 0:
                messages = args[0] if args else kwargs.get("messages", [])
                input_chars = sum(
                    len(
                        str(
                            message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
                        )
                    )
                    for message in messages
                )
                used = max(1, (input_chars + len(str(getattr(response, "content", ""))) + 2) // 3)
            token_usage += used
            messages = args[0] if args else kwargs.get("messages", [])
            normalized_entities = _normalize_extraction_response(
                response,
                _entity_types_from_messages(messages),
            )
            if normalized_entities:
                log.info(
                    "已归一化模型实体类型字段 chunk=%s count=%d",
                    chunk_id,
                    normalized_entities,
                )
            return response

        # zleap-sag 0.7.x 的批处理层会把单块异常记录成失败后返回空列表，调用方
        # 因而无法区分“正常无事项”和“LLM/Schema 失败”。Muse 每次只交给这个
        # extractor 一个 chunk，可以在实例边界记录原始异常并在 extract() 返回后
        # 重新抛出，避免把失败块写入成功断点。无需修改 site-packages。
        original_extract_from_chunk = getattr(extractor, "extract_from_chunk", None)
        if callable(original_extract_from_chunk):

            async def tracked_extract_from_chunk(*args: Any, **kwargs: Any):
                nonlocal chunk_failure
                try:
                    return await original_extract_from_chunk(*args, **kwargs)
                except Exception as error:  # noqa: BLE001 - 保留 SAG 原始异常类型
                    chunk_failure = error
                    raise

            extractor.extract_from_chunk = tracked_extract_from_chunk

        chat_owner.chat = tracked_chat
        try:
            events = await extractor.extract(
                ExtractConfig(
                    source_config_id=self._source_config_id,
                    chunk_ids=[chunk_id],
                    max_concurrency=1,
                    custom_requirements=_KNOWLEDGE_EVENT_REQUIREMENTS,
                    enable_strict_filtering=self._enable_strict_filtering,
                )
            )
            if chunk_failure is not None:
                raise chunk_failure
        finally:
            chat_owner.chat = original_chat
        return [event.id for event in events], token_usage

    async def _restore_checkpoint_events(self, event_ids: list[str]) -> None:
        """分块提交结束后，恢复当前断点已经产出的全部事件。

        zleap-sag 每次保存都会替换整篇文章的事件；断点适配层逐块提交时，
        后提交的块会把先前块的事件标为已删除，因此要按断点统一恢复。
        """
        if not event_ids:
            return
        from sqlalchemy import update
        from zleap.sag.db import SourceEvent, get_session_factory

        unique_ids = list(dict.fromkeys(event_ids))
        session_factory = get_session_factory()
        async with session_factory() as session:
            for offset in range(0, len(unique_ids), 500):
                batch = unique_ids[offset : offset + 500]
                await session.execute(
                    update(SourceEvent)
                    .where(
                        SourceEvent.source_config_id == self._source_config_id,
                        SourceEvent.id.in_(batch),
                        SourceEvent.status == "DELETED",
                    )
                    .values(status="COMPLETED")
                )
            await session.commit()

    async def _normalize_event_ranks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        from sqlalchemy import select
        from zleap.sag.db import SourceEvent, get_session_factory

        chunk_order = {chunk_id: index for index, chunk_id in enumerate(chunk_ids)}
        session_factory = get_session_factory()
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(SourceEvent).where(
                            SourceEvent.source_config_id == self._source_config_id,
                            SourceEvent.chunk_id.in_(chunk_ids),
                        )
                    )
                ).scalars()
            )
            rows.sort(
                key=lambda event: (
                    chunk_order.get(event.chunk_id or "", len(chunk_order)),
                    int(event.rank or 0),
                    event.id,
                )
            )
            for rank, event in enumerate(rows):
                event.rank = rank
            await session.commit()
