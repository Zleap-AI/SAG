"""zleap-sag 的并发、进度和断点适配层。

上游 DataEngine 只暴露整篇 extract；这里把抽取拆成独立 chunk 任务，
每个 chunk 保存成功后立即持久化断点，暂停或重试时从最近确认的断点继续。
"""

from __future__ import annotations

import asyncio
import copy
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
from sag_api.sag.chunk_heading_vectors import complete_loaded_chunk_heading_vectors
from sag_api.sag.dto import ProcessCheckpoint, ProcessOutcome

CheckpointCallback = Callable[[ProcessCheckpoint], Awaitable[None]]
PauseCheck = Callable[[], Awaitable[bool]]
StageCallback = Callable[[str], Awaitable[None]]

log = get_logger("sag.incremental")

_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1

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


def _value_overflows_sqlite_integer(value: object, entity_type: object) -> bool:
    """Match zleap-sag numeric parsing, then check SQLite's signed range."""

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return not _SQLITE_INTEGER_MIN <= value <= _SQLITE_INTEGER_MAX
    if not isinstance(value, str):
        return False
    from zleap.sag.modules.extract.parser import EntityValueParser

    parse = getattr(EntityValueParser, "_sag_original_parse", EntityValueParser.parse)
    parsed = parse(EntityValueParser(), value, entity_type=entity_type if isinstance(entity_type, str) else None)
    return bool(
        parsed
        and parsed.get("type") == "int"
        and not _SQLITE_INTEGER_MIN <= int(parsed["value"]) <= _SQLITE_INTEGER_MAX
    )


def _install_sqlite_integer_guard() -> None:
    """Guard zleap-sag's parser at the same boundary that persists Entity.int_value."""

    from zleap.sag.modules.extract.parser import EntityValueParser

    if getattr(EntityValueParser, "_sag_sqlite_integer_guard_installed", False):
        return
    original_parse = EntityValueParser.parse

    def guarded_parse(self: Any, text: str, *args: Any, **kwargs: Any):
        result = original_parse(self, text, *args, **kwargs)
        if result and result.get("type") == "int" and _value_overflows_sqlite_integer(result.get("value"), None):
            return {**result, "type": "text", "value": str(text), "unit": None}
        return result

    EntityValueParser.parse = guarded_parse
    EntityValueParser._sag_original_parse = original_parse
    EntityValueParser._sag_sqlite_integer_guard_installed = True
    log.warning("已启用 zleap-sag SQLite 整数范围兼容保护")


_install_sqlite_integer_guard()


def _normalize_event_entity_values(event: object) -> int:
    """Downgrade integer entities that SQLite cannot store without losing their text."""

    if not isinstance(event, dict):
        return 0
    normalized = 0
    entities = event.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("value_type") == "text":
                continue
            candidate = entity.get("value") if entity.get("value_type") == "int" else entity.get("name")
            if _value_overflows_sqlite_integer(candidate, entity.get("type")):
                entity["value_type"] = "text"
                normalized += 1

    children = event.get("children")
    if isinstance(children, list):
        for child in children:
            normalized += _normalize_event_entity_values(child)
    return normalized


def _normalize_extraction_response(response: Any, allowed_types: set[str]) -> int:
    """Normalize response fields that would otherwise fail upstream persistence."""

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

    normalized = sum(
        _normalize_event_entity_aliases(item, allowed_types) + _normalize_event_entity_values(item) for item in items
    )
    if normalized:
        response.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return normalized


def _strengthen_event_entity_schema(schema: Any) -> Any:
    """Require an Event body and Entity without mutating PromptManager state.

    ``schema`` may legitimately be ``None`` (or another non-mapping): the
    DeepSeek json_object compatibility path in ``sag.compat`` calls
    ``chat_with_schema(response_schema=None, response_format={"type": "json_object"})``.
    ``strengthened_chat_with_schema`` forwards that ``None`` here, so guard it —
    ``dict(None)`` raises ``'NoneType' object is not iterable`` and crashes the
    whole chunk extraction. Strengthening a null schema is a no-op.
    """

    if not isinstance(schema, Mapping):
        return schema
    strengthened = copy.deepcopy(dict(schema))
    definitions = strengthened.get("definitions")
    event = definitions.get("event") if isinstance(definitions, dict) else None
    if not isinstance(event, dict):
        return strengthened
    required = event.get("required")
    required_fields = list(required) if isinstance(required, list) else []
    for field in ("entities", "content"):
        if field not in required_fields:
            required_fields.append(field)
    event["required"] = required_fields
    properties = event.get("properties")
    entities = properties.get("entities") if isinstance(properties, dict) else None
    if isinstance(entities, dict):
        entities["minItems"] = 1
    content = properties.get("content") if isinstance(properties, dict) else None
    if isinstance(content, dict):
        content["minLength"] = 1
    return strengthened


def _first_task_error(group: BaseExceptionGroup) -> Exception:
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            return _first_task_error(error)
        if isinstance(error, Exception):
            return error
    return RuntimeError(str(group))


class EventEntityContractViolation(ValueError):
    """Raised before persistence when an extracted Event is not exportable."""


def _require_event_entities(events: list[Any]) -> None:
    missing_content = [
        str(getattr(event, "id", "unknown"))
        for event in events
        if not str(getattr(event, "content", "") or "").strip()
    ]
    missing_entities = [
        str(getattr(event, "id", "unknown"))
        for event in events
        if not list(getattr(event, "event_associations", None) or [])
    ]
    problems = []
    if missing_content:
        problems.append("empty content: " + ", ".join(missing_content[:20]))
    if missing_entities:
        problems.append("no valid entity associations: " + ", ".join(missing_entities[:20]))
    if problems:
        raise EventEntityContractViolation(
            "extracted events violate persistence contract; " + "; ".join(problems)
        )


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
        event_entity_attempts: int = 2,
    ) -> None:
        self._engine = engine
        self._source_config_id = source_config_id
        self._max_concurrency = max(1, min(100, max_concurrency))
        self._chunk_max_tokens = chunk_max_tokens
        self._chunk_mode = chunk_mode
        self._document_title = (document_title or "").strip()
        self._enable_strict_filtering = enable_strict_filtering
        self._event_entity_attempts = max(1, min(3, event_entity_attempts))
        self._event_entity_rejection_counts: dict[str, int] = {}

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
            completed_heading_vectors = await complete_loaded_chunk_heading_vectors(
                current.chunk_ids,
                self._source_config_id,
            )
            if completed_heading_vectors:
                log.info(
                    "已为无标题切片补全标题向量 source_config_id=%s count=%d",
                    self._source_config_id,
                    completed_heading_vectors,
                )
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
                        rejected = self._event_entity_rejection_counts.pop(chunk_id, 0)
                        if rejected:
                            quality = current.event_entity_quality
                            quality.rejected_attempts += rejected
                            quality.reason_counts["entities_missing"] = (
                                quality.reason_counts.get("entities_missing", 0)
                                + rejected
                            )
                            if (
                                not event_ids
                                and chunk_id not in quality.eventless_after_contract
                                and len(quality.eventless_after_contract) < 100
                            ):
                                quality.eventless_after_contract.append(chunk_id)
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
        token_usage = 0
        for attempt in range(1, self._event_entity_attempts + 1):
            extractor = EventExtractor(
                prompt_manager=template.prompt_manager,
                model_config=template.model_config,
            )
            chunk_failure: Exception | None = None
            contract_failure: EventEntityContractViolation | None = None
            client = await extractor._get_llm_client()
            chat_owner = _llm_chat_owner(client)
            original_chat = chat_owner.chat
            original_chat_with_schema = getattr(client, "chat_with_schema", None)

            async def tracked_chat(
                *args: Any, _original_chat: Any = original_chat, **kwargs: Any
            ):
                nonlocal token_usage
                response = await _original_chat(*args, **kwargs)
                used = _response_token_usage(response)
                if used <= 0:
                    messages = args[0] if args else kwargs.get("messages", [])
                    input_chars = sum(
                        len(
                            str(
                                message.get("content", "")
                                if isinstance(message, dict)
                                else getattr(message, "content", "")
                            )
                        )
                        for message in messages
                    )
                    used = max(
                        1,
                        (
                            input_chars
                            + len(str(getattr(response, "content", "")))
                            + 2
                        )
                        // 3,
                    )
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

            if callable(original_chat_with_schema):

                async def strengthened_chat_with_schema(
                    *args: Any,
                    _original_chat_with_schema: Any = original_chat_with_schema,
                    **kwargs: Any,
                ) -> Any:
                    call_args = list(args)
                    if "response_schema" in kwargs:
                        kwargs = {
                            **kwargs,
                            "response_schema": _strengthen_event_entity_schema(
                                kwargs["response_schema"]
                            ),
                        }
                    elif len(call_args) >= 2:
                        call_args[1] = _strengthen_event_entity_schema(call_args[1])
                    return await _original_chat_with_schema(*call_args, **kwargs)

                client.chat_with_schema = strengthened_chat_with_schema

            # zleap-sag 0.7.x 的批处理层会吞掉单块异常并返回空列表；记录原始
            # 异常，避免将 LLM/Schema 失败误记为成功的无事项 Chunk。
            original_extract_from_chunk = getattr(extractor, "extract_from_chunk", None)
            if callable(original_extract_from_chunk):

                async def tracked_extract_from_chunk(
                    *args: Any,
                    _original_extract_from_chunk: Any = original_extract_from_chunk,
                    **kwargs: Any,
                ):
                    nonlocal chunk_failure
                    try:
                        return await _original_extract_from_chunk(*args, **kwargs)
                    except Exception as error:  # noqa: BLE001 - 保留 SAG 原始异常类型
                        chunk_failure = error
                        raise

                extractor.extract_from_chunk = tracked_extract_from_chunk

            original_save_events = getattr(extractor, "_save_events", None)
            if callable(original_save_events):

                async def guarded_save_events(
                    events: list[Any],
                    config: ExtractConfig,
                    _original_save_events: Any = original_save_events,
                ):
                    nonlocal contract_failure
                    try:
                        _require_event_entities(events)
                    except EventEntityContractViolation as error:
                        contract_failure = error
                        raise
                    return await _original_save_events(events, config)

                extractor._save_events = guarded_save_events

            requirements = _KNOWLEDGE_EVENT_REQUIREMENTS
            if attempt > 1:
                requirements += (
                    "\n上一次结果包含正文为空或无有效实体的事项。每个事项（包括子事项）"
                    "必须提供非空 content，并至少关联一个可验证实体；否则不要返回该事项。"
                )
            chat_owner.chat = tracked_chat
            try:
                events = await extractor.extract(
                    ExtractConfig(
                        source_config_id=self._source_config_id,
                        chunk_ids=[chunk_id],
                        max_concurrency=1,
                        custom_requirements=requirements,
                        enable_strict_filtering=self._enable_strict_filtering,
                    )
                )
                if chunk_failure is not None:
                    raise chunk_failure
                return [event.id for event in events], token_usage
            except Exception:
                if contract_failure is None:
                    raise
                log.warning(
                    "事项实体契约校验失败 chunk=%s attempt=%d/%d error=%s",
                    chunk_id,
                    attempt,
                    self._event_entity_attempts,
                    contract_failure,
                )
                self._event_entity_rejection_counts[chunk_id] = (
                    self._event_entity_rejection_counts.get(chunk_id, 0) + 1
                )
                if attempt == self._event_entity_attempts:
                    return [], token_usage
            finally:
                chat_owner.chat = original_chat
                if callable(original_chat_with_schema):
                    client.chat_with_schema = original_chat_with_schema

        return [], token_usage

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
